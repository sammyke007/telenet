"""Telenet API Client."""
from __future__ import annotations

import time
from datetime import datetime

from requests import (
    Session,
)

from .const import BASE_HEADERS
from .const import CONNECTION_RETRY
from .const import CONNECTION_RETRY_WAIT
from .const import DATE_FORMAT
from .const import DATETIME_FORMAT
from .const import DEFAULT_LANGUAGE
from .const import DEFAULT_TELENET_ENVIRONMENT
from .const import REQUEST_TIMEOUT
from .exceptions import BadCredentialsException
from .exceptions import TelenetServiceException
from .models import TelenetBundleProductExtraAttributes
from .models import TelenetDtvProductExtraAttributes
from .models import TelenetEnvironment
from .models import TelenetInternetProductExtraAttributes
from .models import TelenetMobileProductExtraAttributes
from .models import TelenetProduct
from .models import TelenetTelephoneProductExtraAttributes
from .utils import clean_ipv6
from .utils import float_to_str
from .utils import float_to_timestring
from .utils import format_entity_name
from .utils import get_json_dict_path
from .utils import get_localized
from .utils import log_debug


class TelenetClient:
    """Telenet client."""

    session: Session
    environment: TelenetEnvironment

    def __init__(
        self,
        session: Session | None = None,
        username: str | None = None,
        password: str | None = None,
        headers: dict | None = BASE_HEADERS,
        language: str | None = DEFAULT_LANGUAGE,
        environment: TelenetEnvironment = DEFAULT_TELENET_ENVIRONMENT,
    ) -> None:
        """Initialize TelenetClient."""
        self.session = session if session else Session()
        self.username = username
        self.password = password
        self.language = language
        self.environment = environment
        self.session.headers = headers
        self.all_products = {}
        self.product_types = []
        self.all_products_by_type = {}
        self.user_details = {}
        self.plan_products = {}
        self.addresses = {}

    def request(
        self,
        url,
        caller="Not set",
        data=None,
        expected="200",
        log=False,
        retrying=False,
        connection_retry_left=CONNECTION_RETRY,
    ) -> dict:
        """Send a request to Telenet."""
        CONNECTION_RETRY
        if data is None:
            log_debug(f"{caller} Calling GET {url}")
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        else:
            log_debug(f"{caller} Calling POST {url}")
            response = self.session.post(url, data, timeout=REQUEST_TIMEOUT)
        log_debug(
            f"{caller} http status code = {response.status_code} (expecting {expected})"
        )
        if log:
            log_debug(f"{caller} Response:\n{response.text}")
        if expected is not None and response.status_code != expected:
            if (
                response.status_code != 403
                and response.status_code != 401
                and response.status_code != 500
                and connection_retry_left > 0
                and not retrying
            ):
                raise TelenetServiceException(
                    f"[{caller}] Expecting HTTP {expected} | Response HTTP {response.status_code}, Response: {response.text}, Url: {response.url}"
                )
            log_debug(
                f"[TelenetClient|request] Received a HTTP {response.status_code}, nothing to worry about! We give it another try :-)"
            )
            self.login()
            time.sleep(CONNECTION_RETRY_WAIT)
            response = self.request(
                url, caller, data, expected, log, True, connection_retry_left - 1
            )
        self.session.headers["X-TOKEN-XSRF"] = self.session.cookies.get("TOKEN-XSRF")
        return response

    def login(self) -> dict:
        """Start a new Telenet session with an user & password."""

        log_debug("[TelenetClient|login|start]")
        tokens = []
        token_fetch_count = 0
        while len(tokens) != 2:
            response = self.request(
                f"{self.environment.ocapi}/oauth/userdetails",
                "[TelenetClient|login]",
                None,
                None,
            )
            if response.status_code == 200:
                # Return if already authenticated
                return response.json()
            if response.status_code != 401 and response.status_code != 403:
                raise TelenetServiceException(
                    f"HTTP {response.status_code} error while authenticating {response.url}"
                )
            """Fetch state & nonce"""
            tokens = response.text.split(",", maxsplit=2)
            if token_fetch_count > CONNECTION_RETRY:
                raise TelenetServiceException(
                    f"HTTP 401 not returning the tokens for {response.url}"
                )
            token_fetch_count += 1
        state, nonce = response.text.split(",", maxsplit=2)
        """Login process"""
        response = self.request(
            f'{self.environment.openid}/oauth/authorize?client_id=ocapi&response_type=code&claims={{"id_token":{{"http://telenet.be/claims/roles":null,"http://telenet.be/claims/licenses":null}}}}&lang=nl&state={state}&nonce={nonce}&prompt=login',
            "[TelenetClient|login|authorize]",
            None,
            None,
        )
        if response.status_code != 200 or "openid/login" not in str(response.url):
            raise TelenetServiceException(response.text())
        response = self.request(
            f"{self.environment.openid}/login.do",
            "[TelenetClient|login|login.do]",
            {
                "j_username": self.username,
                "j_password": self.password,
                "rememberme": True,
            },
            200,
        )
        if "authentication_error" in response.url:
            raise BadCredentialsException(response.text)
        self.session.headers["X-TOKEN-XSRF"] = self.session.cookies.get("TOKEN-XSRF")
        response = self.request(
            "https://api.prd.telenet.be/ocapi/oauth/userdetails",
            "[TelenetClient|login|user_details]",
            None,
            200,
        )
        user_details = response.json()
        if "customer_number" not in user_details:
            raise BadCredentialsException(
                f"HTTP {response.status_code} Missing customer number"
            )
        self.user_details = user_details
        return response.json()

    def add_product_type(self, product_type):
        """Add a discovered product type."""
        if product_type not in self.product_types:
            log_debug(f"[TelenetClient|add_product_type] {product_type}")
            self.product_types.append(product_type)

    def add_product(self, product: dict, plan_identifier: str, state_prop: str) -> bool:
        """Add a discovered product."""
        identifier = product.get("identifier")
        if identifier in self.all_products:
            return False
        type = product.get("productType")
        log_debug(f"[TelenetClient|add_product] {identifier}, productType: {type}")
        if product.get("specurl") is not None:
            product_info = self.product_details(product.get("specurl")).get("product")
        else:
            product_info = {}
        try:
            state = get_localized(
                self.language, product_info.get("localizedcontent")
            ).get("name")
        except Exception:
            state = product.get("label")
        self.all_products[identifier] = TelenetProduct(
            product_identifier=identifier,
            product_type=type,
            product_description_key=type,
            product_plan_identifier=plan_identifier,
            product_name=identifier,
            product_key=format_entity_name(f"{identifier} {type}"),
            product_state=state,
            product_description=product.get("label"),
            product_specurl=product.get("specurl"),
            product_info=product_info,
            product_address=self.address(product.get("addressId")),
            customer_id=self.user_details.get("customer_number"),
        )
        self.add_product_type(type)
        return True

    def products_refreshed(self):
        """Return Telenet products and force the refresh."""
        return self.products(force_refresh=True)

    def products(self, force_refresh=False) -> list:
        """List all Telenet products."""
        if len(self.all_products) > 0 and force_refresh is False:
            """Return the Telenet products present in the Client session"""
            log_debug("[TelenetClient|products] Returning cached products")
            return [self.all_products.get(product) for product in self.all_products]

        log_debug("[TelenetClient|products] Fetching active products from Telenet")
        """ Refresh products """
        self.all_products = {}
        self.product_types = []
        response = self.request(
            "https://api.prd.telenet.be/ocapi/public/api/product-service/v1/products?status=ACTIVE",
            "[TelenetClient|products]",
            None,
            200,
        )
        for a_product in response.json():
            plan_identifier = a_product.get("identifier")
            self.add_product(
                plan_identifier=plan_identifier, product=a_product, state_prop="label"
            )
            dtv_found = False
            log_debug(
                f"[TelenetClient|DEBUG] Parent {a_product.get('identifier')} {a_product.get('productType')}"
            )
            for product in a_product.get("children"):
                log_debug(
                    f"[TelenetClient|DEBUG] Child {product.get('identifier')} {product.get('productType')}"
                )
                if product.get("productType") == "dtv":
                    dtv_found = True
                if "options" in product and len(product.get("options")):
                    for option in product.get("options"):
                        if "identifier" in option:
                            self.add_product(
                                product=option,
                                plan_identifier=plan_identifier,
                                state_prop="label",
                            )

                self.add_product(
                    product=product, plan_identifier=plan_identifier, state_prop="label"
                )
            if dtv_found and a_product.get("productType") == "dtv":
                log_debug("[TelenetClient|products] DTV child found & ignoring")
                self.all_products.get(
                    plan_identifier
                ).product_ignore_extra_sensor = True
        self.product_subscriptions()
        self.plan_info()
        self.create_extra_sensors()
        self.set_extra_attributes()
        return [self.all_products.get(product) for product in self.all_products]

    def construct_extra_sensor(
        self,
        product,
        suffix,
        product_description_key,
        product_state,
        product_extra_attributes={},
        use_plan_identifier=False,
        native_unit_of_measurement=None,
    ) -> list:
        """For each found product add extra product sensors."""
        type = product.product_type
        identifier = product.product_identifier
        plan_identifier = product.product_plan_identifier
        if use_plan_identifier:
            identifier = plan_identifier
        product_key = format_entity_name(
            f"{identifier} {type} {suffix}"
        )
        return {
            product_key: TelenetProduct(
                product_identifier=f"{identifier} {suffix}",
                product_type=type,
                product_description_key=product_description_key,
                product_suffix=suffix,
                product_plan_identifier=plan_identifier,
                product_name=f"{identifier} {suffix}",
                product_key=product_key,
                product_state=product_state,
                product_extra_sensor=True,
                product_extra_attributes=product_extra_attributes,
                native_unit_of_measurement=native_unit_of_measurement,
            )
        }

    def create_extra_sensors(self) -> bool:
        """Create extra sensors."""
        new_products = {}
        for product in self.all_products:
            product = self.all_products[product]
            type = product.product_type
            identifier = product.product_identifier
            plan_identifier = product.product_plan_identifier
            product_specs = self.product_details(product.product_specurl).get("product")
            log_debug(f"[TelenetClient|create_extra_sensors] {identifier} {type}")
            if type == "internet":
                """------------------------"""
                """| EXTRA INTERNET SENSORS |"""
                """ ------------------------ """
                billcycle = self.bill_cycles(type, identifier, 2)
                product_usage = self.product_usage(
                    type,
                    identifier,
                    billcycle.get("start_date"),
                    billcycle.get("end_date"),
                )
                daily_peak = []
                daily_off_peak = []
                daily_total = []
                daily_date = []
                product_daily_usage = {}
                for cycle in billcycle.get('cycles'):
                    product_daily_usage |= {
                        cycle.get("billCycle"): self.product_daily_usage(
                            type,
                            identifier,
                            cycle.get("billCycle"),
                            cycle.get("startDate"),
                            cycle.get("endDate"),
                        )
                    }
                    for day in product_daily_usage.get(cycle.get("billCycle")).get('internetUsage')[0].get('dailyUsages'):
                        daily_peak.append(day.get('peak'))
                        daily_off_peak.append(day.get('offPeak'))
                        daily_total.append(day.get('total'))
                        daily_date.append(day.get('date'))

                product_daily_usage = product_daily_usage.get('CURRENT')
                modem = self.modems(identifier)
                wireless_settings = self.wireless_settings(modem.get("mac"), identifier)
                wifi_qr = None
                usage = product_usage.get(type)
                usage_pct = (
                    100
                    * usage.get("totalUsage").get("units")
                    / (
                        usage.get("allocatedUsage").get("units")
                        + usage.get("extendedUsage").get("volume")
                    )
                )
                period_length = datetime.strptime(
                    billcycle.get("end_date"), DATE_FORMAT
                ) - datetime.strptime(billcycle.get("start_date"), DATE_FORMAT)
                period_length_days = period_length.days
                period_length_seconds = period_length.total_seconds()
                period_used = datetime.now() - datetime.strptime(
                    billcycle.get("start_date"), DATE_FORMAT
                )
                period_used_seconds = period_used.total_seconds()
                period_used_percentage = round(
                    100 * period_used_seconds / period_length_seconds, 1
                )
                attributes = {
                    "identifier": identifier,
                    "last_update": usage.get("totalUsage").get("lastUsageDate"),
                    "start_date": billcycle.get("start_date"),
                    "end_date": billcycle.get("end_date"),
                    "days_until": usage.get("daysUntil"),
                    "total_usage": f"{usage.get('totalUsage').get('units')} {usage.get('totalUsage').get('unitType')}",
                    "wifree_usage": f"{usage.get('wifreeUsage').get('usedUnits')} {usage.get('wifreeUsage').get('unitType')}",
                    "allocated_usage": f"{usage.get('allocatedUsage').get('units')} {usage.get('allocatedUsage').get('unitType')}",
                    "extended_usage": f"{usage.get('extendedUsage').get('volume')} {usage.get('extendedUsage').get('unit')}",
                    "extended_usage_price": f"{usage.get('extendedUsage').get('price')} {usage.get('extendedUsage').get('currency')}",
                    "peak_usage": usage.get("peakUsage").get("usedUnits"),
                    "offpeak_usage": round(
                        get_json_dict_path(
                            product_daily_usage, "$.internetUsage[0].totalUsage.offPeak"
                        ),
                        1,
                    ),
                    "total_usage_with_offpeak": usage.get("peakUsage").get("usedUnits")
                    + round(
                        get_json_dict_path(
                            product_daily_usage, "$.internetUsage[0].totalUsage.offPeak"
                        ),
                        1,
                    ),
                    "used_percentage": round(usage_pct, 2),
                    "period_used_percentage": period_used_percentage,
                    "period_remaining_percentage": (100 - period_used_percentage),
                    "squeezed": usage_pct >= 100,
                    "period_length": period_length_days,
                    "product_label": f"{get_localized(self.language, product_specs.get('localizedcontent')).get('name')}",
                    "sales_price": f"{product_specs.get('characteristics').get('salespricevatincl').get('value')} {product_specs.get('characteristics').get('salespricevatincl').get('unit')}",
                }
                service = ""
                for services in product_specs.get("services"):
                    for specification in services.get("specifications"):
                        if (
                            specification.get("labelkey")
                            == "spec.fixedinternet.speed.download"
                        ):
                            attributes[
                                "download_speed"
                            ] = f"{specification.get('value')} {specification.get('unit')}"
                        elif (
                            specification.get("labelkey")
                            == "spec.fixedinternet.speed.upload"
                        ):
                            attributes[
                                "upload_speed"
                            ] = f"{specification.get('value')} {specification.get('unit')}"
                        if specification.get("visible"):
                            service += f"{get_localized(self.language, specification.get('localizedcontent')).get('name')}"
                            if specification.get("value") is not None:
                                service += f" {specification.get('value')}"
                            if specification.get("unit") is not None:
                                service += f" {specification.get('unit')}"
                            service += "\n"
                if usage_pct >= 100:
                    attributes["download_speed"] = "1 Mbps"
                    attributes["upload_speed"] = "256 Kbps"
                attributes["service"] = service

                new_products.update(
                    self.construct_extra_sensor(
                        product,
                        "usage",
                        "usage_percentage",
                        usage_pct,
                        attributes,
                    )
                )
                new_products.update(
                    self.construct_extra_sensor(
                        product,
                        "daily usage",
                        "data_usage",
                        get_json_dict_path(
                            product_daily_usage, "$.internetUsage[0].totalUsage.peak"
                        ),
                        self.create_extra_attributes_list(
                            get_json_dict_path(
                                product_daily_usage, "$.internetUsage[0].totalUsage"
                            )
                        )|{
                            "daily_peak": daily_peak,
                            "daily_off_peak": daily_off_peak,
                            "daily_total": daily_total,
                            "daily_date": daily_date,
                        },
                    )
                )
                new_products.update(
                    self.construct_extra_sensor(
                        product,
                        "modem",
                        "modem",
                        modem.get("name"),
                        self.create_extra_attributes_list(modem),
                    )
                )
                network_topology = clean_ipv6(self.network_topology(modem.get("mac")))
                new_products.update(
                    self.construct_extra_sensor(
                        product,
                        "network",
                        "network",
                        network_topology.get("model"),
                        self.create_extra_attributes_list(network_topology),
                    )
                )
                new_products.update(
                    self.construct_extra_sensor(
                        product,
                        "wifi",
                        "wifi",
                        wireless_settings.get("wirelessEnabled"),
                        self.create_extra_attributes_list(network_topology),
                    )
                )
                if "networkKey" in wireless_settings.get("singleSSIDRoamingSettings"):
                    network_key = (
                        wireless_settings.get("singleSSIDRoamingSettings")
                        .get("networkKey")
                        .replace(":", r"\:")
                    )
                    wifi_qr = f"WIFI:S:{wireless_settings.get('singleSSIDRoamingSettings').get('name')};T:WPA;P:{network_key};;"
                    new_products.update(
                        self.construct_extra_sensor(product, "qr", "qr", wifi_qr)
                    )
            elif type == "dtv":
                """-------------------"""
                """| EXTRA DTV SENSORS |"""
                """ ------------------- """
                if not product.product_ignore_extra_sensor:
                    billcycle = self.bill_cycles(type, identifier, 1)
                    product_usage = self.product_usage(
                        type,
                        identifier,
                        billcycle.get("start_date"),
                        billcycle.get("end_date"),
                    )
                    devices = self.device_details(type, identifier)
                    new_products.update(
                        self.construct_extra_sensor(
                            product,
                            "usage",
                            "euro",
                            get_json_dict_path(
                                product_usage, "$.dtv.totalUsage.currentUsage"
                            ),
                            self.create_extra_attributes_list(
                                get_json_dict_path(product_usage, "$.dtv")
                            ),
                        )
                    )
                    for idx, _data in enumerate(devices.get("dtv")):
                        new_products.update(
                            self.construct_extra_sensor(
                                product,
                                "dtv",
                                "dtv",
                                get_json_dict_path(devices, f"$.dtv[{idx}].boxName"),
                                self.create_extra_attributes_list(
                                    get_json_dict_path(devices, f"$.dtv[{idx}]")
                                ),
                            )
                        )
            elif type == "mobile":
                """----------------------"""
                """| EXTRA MOBILE SENSORS |"""
                """ ---------------------- """
                if plan_identifier != identifier:
                    bundle_key = format_entity_name(
                        f"{self.user_details.get('identity_id')} {plan_identifier} {type} bundle"
                    )
                    usage = self.mobile_bundle_usage(plan_identifier, identifier)
                    next_billing_date = usage.get("nextBillingDate")
                    next_billing_date_time = datetime.strptime(
                        usage.get("nextBillingDate"), DATETIME_FORMAT
                    ).replace(tzinfo=None)
                    days_until = (next_billing_date_time - datetime.now()).days
                    attr_to_merge = {
                        "days_until": days_until,
                        "next_billing_date": next_billing_date,
                    }
                    bundleusage = self.mobile_bundle_usage(plan_identifier)
                    if self.all_products.get(bundle_key) is None:
                        """Bundle mobile sensors"""
                        log_debug(
                            f"[TelenetClient|create_extra_sensors] Create Bundle Sensor BundleId: {plan_identifier}"
                        )
                        new_products.update(
                            self.construct_extra_sensor(
                                product,
                                "out of bundle",
                                "euro",
                                float_to_str(
                                    get_json_dict_path(
                                        bundleusage, "$.outOfBundle.usedUnits"
                                    )
                                ),
                                self.create_extra_attributes_list(
                                    get_json_dict_path(bundleusage, "$.outOfBundle")
                                )
                                | attr_to_merge,
                                use_plan_identifier=True,
                            )
                        )
                        for data in bundleusage.get("shared").get("data"):
                            new_products.update(
                                self.construct_extra_sensor(
                                    product,
                                    data.get("bucketType"),
                                    "usage_percentage_mobile",
                                    data.get("usedPercentage"),
                                    {
                                        "usage": f"{data.get('usedUnits')}/{data.get('startUnits')} {data.get('unitType')}"
                                    }
                                    | data
                                    | attr_to_merge,
                                    use_plan_identifier=True,
                                )
                            )
                        for data in bundleusage.get("shared").get("text"):
                            new_products.update(
                                self.construct_extra_sensor(
                                    product,
                                    "sms",
                                    "mobile_sms",
                                    data.get("usedUnits"),
                                    {"usage": f"{data.get('usedUnits')} SMSes"} | data,
                                    use_plan_identifier=True,
                                )
                            )
                        for data in bundleusage.get("shared").get("voice"):
                            new_products.update(
                                self.construct_extra_sensor(
                                    product,
                                    "voice",
                                    "mobile_voice",
                                    float_to_timestring(
                                        data.get("usedUnits"), data.get("unitType")
                                    ),
                                    {
                                        "usage": float_to_timestring(
                                            data.get("usedUnits"), data.get("unitType")
                                        )
                                    }
                                    | data
                                    | attr_to_merge,
                                    use_plan_identifier=True,
                                )
                            )
                    """ Child mobile sensors """
                    new_products.update(
                        self.construct_extra_sensor(
                            product,
                            "out of bundle",
                            "euro",
                            float_to_str(
                                get_json_dict_path(usage, "$.outOfBundle.usedUnits")
                            ),
                            self.create_extra_attributes_list(
                                get_json_dict_path(usage, "$.outOfBundle")
                            )
                            | attr_to_merge,
                        )
                    )
                    for data in usage.get("shared").get("data"):
                        new_products.update(
                            self.construct_extra_sensor(
                                product,
                                data.get("name").lower(),
                                "mobile_data",
                                float_to_str(data.get("usedUnits")),
                                {
                                    "usage": f"{data.get('usedUnits')} {data.get('unitType')}"
                                }
                                | data
                                | attr_to_merge,
                                False,
                                data.get("unitType"),
                            )
                        )
                    for data in usage.get("shared").get("text"):
                        new_products.update(
                            self.construct_extra_sensor(
                                product,
                                data.get("name").lower().replace("text", "sms"),
                                "mobile_sms",
                                data.get("usedUnits"),
                                {"usage": f"{data.get('usedUnits')} SMSes"}
                                | data
                                | attr_to_merge,
                            )
                        )
                    for data in usage.get("shared").get("voice"):
                        new_products.update(
                            self.construct_extra_sensor(
                                product,
                                data.get("name").lower(),
                                "mobile_voice",
                                float_to_timestring(
                                    data.get("usedUnits"), data.get("unitType")
                                ),
                                {
                                    "usage": float_to_timestring(
                                        data.get("usedUnits"), data.get("unitType")
                                    )
                                }
                                | data
                                | attr_to_merge,
                            )
                        )
                else:
                    log_debug(
                        f"[TelenetClient|MOBILE] {type} BundleId: {plan_identifier}, id: {identifier}, {product.product_description_key}"
                    )
                    usage = self.mobile_usage(identifier)
                    next_billing_date = usage.get("nextBillingDate")
                    next_billing_date_time = datetime.strptime(
                        usage.get("nextBillingDate"), DATETIME_FORMAT
                    ).replace(tzinfo=None)
                    days_until = (next_billing_date_time - datetime.now()).days
                    attr_to_merge = {
                        "days_until": days_until,
                        "next_billing_date": next_billing_date,
                    }
                    """ Non bundle mobile sensors """
                    new_products.update(
                        self.construct_extra_sensor(
                            product,
                            "out of bundle",
                            "euro",
                            float_to_str(
                                get_json_dict_path(usage, "$.outOfBundle.usedUnits")
                            ),
                            self.create_extra_attributes_list(
                                get_json_dict_path(usage, "$.outOfBundle")
                            )
                            | attr_to_merge,
                            use_plan_identifier=True,
                        )
                    )
                    data = usage.get("total").get("data")
                    if (
                        int(data.get("startUnits")) > 0
                        or int(data.get("remainingUnits")) > 0
                        or int(data.get("usedUnits")) > 0
                    ):
                        new_products.update(
                            self.construct_extra_sensor(
                                product,
                                "data",
                                "mobile_data",
                                float_to_str(data.get("usedUnits")),
                                {
                                    "usage": f"{data.get('usedUnits')} {data.get('unitType')}"
                                }
                                | data
                                | attr_to_merge,
                                False,
                                data.get("unitType"),
                            )
                        )
                    data = usage.get("total").get("text")
                    if (
                        int(data.get("startUnits")) > 0
                        or int(data.get("remainingUnits")) > 0
                        or int(data.get("usedUnits")) > 0
                    ):
                        new_products.update(
                            self.construct_extra_sensor(
                                product,
                                "sms",
                                "mobile_sms",
                                data.get("usedUnits"),
                                {
                                    "usage": f"{data.get('usedUnits')} / {data.get('startUnits')} SMSes"
                                }
                                | data
                                | attr_to_merge,
                            )
                        )
                    data = usage.get("total").get("voice")
                    if (
                        int(data.get("startUnits")) > 0
                        or int(data.get("remainingUnits")) > 0
                        or int(data.get("usedUnits")) > 0
                    ):
                        new_products.update(
                            self.construct_extra_sensor(
                                product,
                                "sms",
                                "mobile_voice",
                                float_to_timestring(
                                    data.get("usedUnits"), data.get("unitType")
                                ),
                                {
                                    "usage": f"{data.get('usedUnits')} / {data.get('startUnits')} {data.get('unitType').lower()}"
                                }
                                | data
                                | attr_to_merge,
                            )
                        )

        self.all_products.update(new_products)
        return True

    def create_extra_attributes_list(self, attr_list):
        """Create extra attributes for a sensor."""
        attributes = {}
        for key in attr_list:
            attributes[key] = attr_list[key]
        return attributes

    def set_extra_attributes(self) -> bool:
        """Set extra attributes per product."""
        for product in self.all_products:
            product = self.all_products[product]
            if not product.product_extra_sensor:
                if len(product.product_subscription_info) > 0:
                    info = product.product_subscription_info
                else:
                    info = self.plan_products.get(product.product_identifier)
                log_debug(
                    f"[TelenetClient|set_extra_attributes] Setting extra attributes for {product.product_identifier} Length: {len(info)}"
                )

                extra_attributes = {}
                if product.product_type == "internet":
                    attributes = TelenetInternetProductExtraAttributes()
                elif product.product_type == "mobile":
                    attributes = TelenetMobileProductExtraAttributes()
                elif product.product_type == "dtv":
                    attributes = TelenetDtvProductExtraAttributes()
                elif product.product_type == "telephone":
                    attributes = TelenetTelephoneProductExtraAttributes()
                elif product.product_type == "bundle":
                    attributes = TelenetBundleProductExtraAttributes()
                for key in dir(attributes):
                    if key[0:2] != "__":
                        if key in info:
                            extra_attributes[key] = info.get(key)
                product.product_extra_attributes |= extra_attributes
        return True

    def product_details(self, url):
        """Fetch product_details."""
        response = self.request(url, "product_details", None, 200)
        return response.json()

    def plan_info(self):
        """Fetch PLAN product subscriptions."""
        self.plan_products = {}
        log_debug("[TelenetClient|plan_info] Fetching plan info from Telenet")
        response = self.request(
            "https://api.prd.telenet.be/ocapi/public/api/product-service/v1/product-subscriptions?producttypes=PLAN",
            "[TelenetClient|planInfo]",
            None,
            200,
        )
        for plan in response.json():
            self.plan_products[plan.get("identifier")] = plan
        return False

    def bill_cycles(self, product_type, product_identifier, count=1):
        """Fetch bill cycles."""
        log_debug(
            f"[TelenetClient|bill_cycle] Fetching bill_cycles info from Telenet for {product_identifier} ({product_type})"
        )
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/billing-service/v1/account/products/{product_identifier}/billcycle-details?producttype={product_type}&count={count}",
            "[TelenetClient|bill_cycles]",
            None,
            200
        )
        cycle = response.json().get("billCycles")[0]
        if product_type == "internet":
            return {"start_date": cycle.get("startDate"), "end_date": cycle.get("endDate"), "cycles": response.json().get("billCycles")}
        else:
            return {"start_date": cycle.get("startDate"), "end_date": cycle.get("endDate")}

    def product_usage(self, product_type, product_identifier, startDate, endDate):
        """Fetch product_usage."""
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/product-service/v1/products/{product_type}/{product_identifier}/usage?fromDate={startDate}&toDate={endDate}",
            "[TelenetClient|product_usage]",
            None,
            200
        )
        return response.json()

    def product_daily_usage(self, product_type, product_identifier, bill_cycle, from_date, to_date):
        """Fetch daily usage."""
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/product-service/v1/products/{product_type}/{product_identifier}/dailyusage?billcycle={bill_cycle}&fromDate={from_date}&toDate={to_date}",
            "[TelenetClient|product_daily_usage]",
            None,
            None
        )
        if response.status_code != 200:
            return {}
        return response.json()

    def product_subscriptions(self):
        """Fetch product subscriptions for all product types."""
        for product_type in self.product_types:
            log_debug(
                f"[TelenetClient|product_subscriptions] Fetching product plan infos from Telenet for {product_type}"
            )
            response = self.request(
                f"https://api.prd.telenet.be/ocapi/public/api/product-service/v1/product-subscriptions?producttypes={product_type.upper()}",
                "[TelenetClient|product_subscriptions]",
                None,
                200,
            )
            for product in response.json():
                self.all_products[
                    product.get("identifier")
                ].product_subscription_info = product

    def mobile_usage(self, product_identifier):
        """Fetch mobile usage."""
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/mobile-service/v3/mobilesubscriptions/{product_identifier}/usages",
            "[TelenetClient|mobile_usage]",
            None,
            200,
        )
        return response.json()

    def mobile_bundle_usage(self, bundle_identifier, line_identifier=None):
        """Fetch mobile bundle usage."""
        if line_identifier is not None:
            response = self.request(
                f"https://api.prd.telenet.be/ocapi/public/api/mobile-service/v3/mobilesubscriptions/{bundle_identifier}/usages?type=bundle&lineIdentifier={line_identifier}",
                "[TelenetClient|mobile_bundle_usage line_identifier]",
                None,
                200,
            )
        else:
            response = self.request(
                f"https://api.prd.telenet.be/ocapi/public/api/mobile-service/v3/mobilesubscriptions/{bundle_identifier}/usages?type=bundle",
                "[TelenetClient|mobile_bundle_usage bundle]",
                None,
                200,
            )
        return response.json()

    def modems(self, product_identifier):
        """Fetch modem info."""
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/resource-service/v1/modems?productIdentifier={product_identifier}",
            "[TelenetClient|modems]",
            None,
            200,
        )
        return response.json()

    def network_topology(self, mac):
        """Fetch network topology."""
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/resource-service/v1/network-topology/{mac}?withClients=true",
            "[TelenetClient|network_topology]",
            None,
            200,
        )
        return response.json()

    def wireless_settings(self, mac, product_identifier):
        """Fetch wireless settings."""
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/resource-service/v1/modems/{mac}/wireless-settings?withmetadata=true&withwirelessservice=true&productidentifier={product_identifier}",
            "[TelenetClient|wireless_settings]",
            None,
            200,
        )
        return response.json()

    def device_details(self, product_type, product_identifier):
        """Fetch device details."""
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/product-service/v1/products/{product_type}/{product_identifier}/devicedetails",
            "[TelenetClient|device_details]",
            None,
            200,
        )
        return response.json()

    def address(self, address_id):
        """Fetch address."""
        log_debug(f"[TelenetClient|address] Fetching address {address_id}")
        if address_id is None or len(address_id) == 0:
            return {}
        if self.addresses.get(address_id) is not None:
            return self.addresses.get(address_id)
        response = self.request(
            f"https://api.prd.telenet.be/ocapi/public/api/contact-service/v1/contact/addresses/{address_id}",
            "[TelenetClient|address]",
            None,
            200,
        )
        self.addresses |= {address_id: response.json()}
        return response.json()
