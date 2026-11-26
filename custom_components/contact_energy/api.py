"""Contact Energy API."""

import datetime as dt
import logging
import time

import requests

_LOGGER = logging.getLogger(__name__)

# Shared platform key extracted from myaccount.contact.co.nz — public, not a user secret,
# but may be rotated by Contact Energy without notice.
CONTACT_ENERGY_API_KEY = "z840P4lQCH9TqcjC9L2pP157DZcZJMcr5tVQCvyx"

# Responses that mean the login token is no longer accepted.
_AUTH_REJECTED = (requests.codes.unauthorized, requests.codes.forbidden)

# Contact answers an expired login token with HTTP 502, not 401/403, so the
# token cannot be detected as rejected. Sign in again once it is this old
# (seconds), and once more after a 5xx unless it was issued just now.
_TOKEN_MAX_AGE = 30 * 60
_TOKEN_JUST_ISSUED = 60


class ContactEnergyApi:
    """Class for Contact Energy API."""

    def __init__(self, email, password):
        """Initialise Contact Energy API."""
        self._api_token = ""
        self._api_session = ""
        self._contractId = ""
        self._gas_contract_id = ""
        self._accountId = ""
        self._url_base = "https://api.contact-digital-prod.net"
        self._email = email
        self._password = password
        # monotonic time of the last successful login; None until then
        self._token_time = None

    def login(self):
        """Login to the Contact Energy API."""
        headers = {"x-api-key": CONTACT_ENERGY_API_KEY}
        data = {"username": self._email, "password": self._password}
        try:
            result = requests.post(
                self._url_base + "/login/v2",
                json=data,
                headers=headers,
                timeout=(10, 30),
            )
        except requests.RequestException as err:
            # Only the exception type is logged: its text can include the request URL.
            _LOGGER.error("Failed to login: %s", type(err).__name__)
            return False
        if result.status_code == requests.codes.ok:
            try:
                self._api_token = result.json()["token"]
            except ValueError, KeyError, TypeError:
                _LOGGER.error("Failed to login: unexpected response")
                return False
            self._token_time = time.monotonic()
            _LOGGER.debug("Logged in")
            self.refresh_session()
            return True
        _LOGGER.error("Failed to login: HTTP %s", result.status_code)
        return False

    def refresh_session(self):
        """Refresh the session."""
        headers = {"x-api-key": CONTACT_ENERGY_API_KEY}
        data = {"username": self._email, "password": self._password}
        try:
            result = requests.post(
                self._url_base + "/login/v2/refresh",
                json=data,
                headers=headers,
                timeout=(10, 30),
            )
        except requests.RequestException as err:
            _LOGGER.error("Failed to refresh session: %s", type(err).__name__)
            return False
        if result.status_code == requests.codes.ok:
            try:
                self._api_session = result.json()["session"]
            except ValueError, KeyError, TypeError:
                _LOGGER.error("Failed to refresh session: unexpected response")
                return False
            _LOGGER.debug("Refreshed session")
            self.get_accounts()
            return True
        _LOGGER.error("Failed to refresh session: HTTP %s", result.status_code)
        return False

    def get_accounts(self):
        """Get the first account."""
        headers = {"x-api-key": CONTACT_ENERGY_API_KEY, "session": self._api_session}
        try:
            result = requests.get(
                self._url_base + "/customer/v2?fetchAccounts=true",
                headers=headers,
                timeout=(10, 30),
            )
        except requests.RequestException as err:
            _LOGGER.error("Failed to fetch customer accounts: %s", type(err).__name__)
            return False
        if result.status_code == requests.codes.ok:
            try:
                data = result.json()
                account_id = data["accounts"][0]["id"]
                contracts = data["accounts"][0]["contracts"]
                contract_id = contracts[0]["contractId"]
            except ValueError, KeyError, IndexError, TypeError:
                _LOGGER.error("Customer has no usable account or contract")
                return False
            ids = [c.get("contractId") for c in contracts if isinstance(c, dict)]
            ids = [i for i in ids if i]
            electricity, gas = contract_id, ""
            if len(ids) > 1 and contract_id == ids[0]:
                gas = ids[1]
            # The API has no field distinguishing electricity from gas, and the
            # contracts are not in a fixed order: on some dual-fuel accounts
            # the gas contract comes first. Only the electricity contract
            # serves hourly usage, so probe for it; unless exactly one
            # contract does (e.g. the API is down), keep the listed order.
            # The probe needs the account id, but the contracts are only
            # assigned once it is done: both sensors share this object and
            # must not see a half-finished pick.
            self._accountId = account_id
            hourly = self._find_hourly_contract(ids) if len(ids) > 1 else ""
            if hourly:
                electricity = hourly
                others = [i for i in ids if i != hourly]
                gas = others[0] if others else ""
            self._contractId, self._gas_contract_id = electricity, gas
            _LOGGER.debug("Retrieved accounts")
            return True
        _LOGGER.error("Failed to fetch customer accounts: HTTP %s", result.status_code)
        return False

    def _find_hourly_contract(self, contract_ids):
        """Return the one contract that serves hourly usage, or "" if unsure.

        Probes a day far enough back to have been published (Contact Energy
        runs about two days late). Gas contracts answer HTTP 502 here. A
        contract only counts if it returns usage data, and only when exactly
        one contract does: a one-off failure on the electricity contract must
        not swap the two.
        """
        day = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        serving = []
        for contract_id in contract_ids:
            try:
                response = self._post_usage(contract_id, "hourly", day, day)
            except requests.RequestException as err:
                _LOGGER.debug("Contract probe failed: %s", type(err).__name__)
                continue
            if response.status_code != requests.codes.ok:
                continue
            try:
                data = response.json()
            except ValueError:
                continue
            if isinstance(data, list) and data:
                serving.append(contract_id)
        if len(serving) == 1:
            return serving[0]
        if len(serving) > 1:
            _LOGGER.debug("Several contracts serve hourly usage; keeping listed order")
        return ""

    def _token_age(self):
        """Seconds since the last login, or None if the token's age is unknown."""
        if self._token_time is None:
            return None
        return time.monotonic() - self._token_time

    def _post_usage(self, contract_id, interval, date_from, date_to):
        """POST a usage request, signing in again if the token may have expired.

        Raises requests.RequestException like requests.post.
        """
        url = (
            f"{self._url_base}/usage/v2/{contract_id}"
            f"?ba={self._accountId}&interval={interval}&from={date_from}&to={date_to}"
        )
        age = self._token_age()
        if self._api_token and age is not None and age > _TOKEN_MAX_AGE:
            _LOGGER.debug("Login token is %d s old; signing in again", age)
            self.login()
        response = requests.post(url, headers=self._usage_headers(), timeout=(10, 30))
        age = self._token_age()
        if response.status_code >= 500 and age is not None and age > _TOKEN_JUST_ISSUED:
            _LOGGER.info(
                "Usage request returned HTTP %s; signing in again and retrying",
                response.status_code,
            )
            if self.login():
                response = requests.post(
                    url, headers=self._usage_headers(), timeout=(10, 30)
                )
        return response

    def _usage_headers(self):
        return {
            "x-api-key": CONTACT_ENERGY_API_KEY,
            "authorization": self._api_token,
        }

    def get_usage(self, year, month, day):
        """Fetch hourly usage data for a single day.

        Returns the data, or False on any failure. A rejected token is cleared so
        the caller knows to log in again.
        """
        if not self._contractId or not self._accountId:
            _LOGGER.error("No account or contract known yet; cannot fetch usage")
            return False
        date = f"{year}-{str(month).zfill(2)}-{str(day).zfill(2)}"
        try:
            response = self._post_usage(self._contractId, "hourly", date, date)
        except requests.RequestException as err:
            _LOGGER.error(
                "Failed to fetch usage for %s/%s/%s: %s",
                year,
                month,
                day,
                type(err).__name__,
            )
            return False
        if response.status_code == requests.codes.ok:
            try:
                data = response.json()
            except ValueError:
                _LOGGER.error(
                    "Failed to fetch usage for %s/%s/%s: unexpected response",
                    year,
                    month,
                    day,
                )
                return False
            if not data:
                _LOGGER.info("No usage data returned for %s/%s/%s", year, month, day)
            return data
        if response.status_code in _AUTH_REJECTED:
            _LOGGER.info("Login token rejected; will log in again")
            self._api_token = ""
        _LOGGER.error(
            "Failed to fetch usage for %s/%s/%s: HTTP %s",
            year,
            month,
            day,
            response.status_code,
        )
        return False

    def get_gas_usage(self, date_from, date_to):
        """Fetch monthly gas usage data (kWh) for the gas contract, if present.

        Gas meters are read monthly rather than hourly, and Contact Energy's
        API does not return a dollarValue for gas usage points. Returns the
        data, or False on any failure (a rejected token is cleared, as for
        `get_usage`).
        """
        if not self._gas_contract_id or not self._accountId:
            return False
        try:
            response = self._post_usage(
                self._gas_contract_id, "monthly", date_from, date_to
            )
        except requests.RequestException as err:
            _LOGGER.error(
                "Failed to fetch gas usage for %s to %s: %s",
                date_from,
                date_to,
                type(err).__name__,
            )
            return False
        if response.status_code == requests.codes.ok:
            try:
                data = response.json()
            except ValueError:
                _LOGGER.error(
                    "Failed to fetch gas usage for %s to %s: unexpected response",
                    date_from,
                    date_to,
                )
                return False
            if not data:
                _LOGGER.info(
                    "No gas usage data returned for %s to %s", date_from, date_to
                )
            return data
        if response.status_code in _AUTH_REJECTED:
            _LOGGER.info("Login token rejected; will log in again")
            self._api_token = ""
        _LOGGER.error(
            "Failed to fetch gas usage for %s to %s: HTTP %s",
            date_from,
            date_to,
            response.status_code,
        )
        return False