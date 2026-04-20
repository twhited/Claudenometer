"""
Cronometer GWT-RPC client.

Cronometer has no public API; it communicates via GWT-RPC over HTTPS.
Authentication requires fetching a CSRF token from the login page, submitting
credentials via form POST to /login, then calling GWT-RPC authenticate to get
a user_id and sesnonce cookie.

Protocol summary:
  - Step 1: GET /login/ → extract anticsrf token
  - Step 2: POST /login with credentials + anticsrf → get sesnonce cookie
  - Step 3: POST /cronometer/app with GWT authenticate payload → get user_id
  - Step 4: POST /cronometer/app for any RPC operation (findFoods, updateDiary …)
  - Two GWT values are needed:
      gwt_permutation  32-char hex from cronometer.nocache.js (X-GWT-Permutation header)
      gwt_header       32-char hex from {permutation}.cache.js (inside the payload body)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import pickle
import re
from datetime import date
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOGIN_HTML_URL = "https://cronometer.com/login/"
LOGIN_API_URL = "https://cronometer.com/login"
GWT_BASE_URL = "https://cronometer.com/cronometer/app"
EXPORT_URL = "https://cronometer.com/export"
GWT_NOCACHE_JS_URL = "https://cronometer.com/cronometer/cronometer.nocache.js"
GWT_CACHE_JS_URL = "https://cronometer.com/cronometer/{permutation}.cache.js"

GWT_CONTENT_TYPE = "text/x-gwt-rpc; charset=UTF-8"
GWT_MODULE_BASE = "https://cronometer.com/cronometer/"

# Fallback GWT magic values — updated when Cronometer redeploys their frontend.
DEFAULT_GWT_PERMUTATION = "CBC38FBB0A1527BD5E68722DD9DABD27"
DEFAULT_GWT_HEADER = "76FC4464E20E53D16663AC9A96A486B3"

# Using an NCCDB measure_id that works for any food source; CRDB-specific
# measure_ids cause ghost entries (counted but invisible in the diary).
UNIVERSAL_MEASURE_ID = 124399

# ---------------------------------------------------------------------------
# GWT payload templates
# ---------------------------------------------------------------------------

GWT_AUTHENTICATE = (
    "7|0|5|https://cronometer.com/cronometer/|"
    "{gwt_header}|"
    "com.cronometer.shared.rpc.CronometerService|"
    "authenticate|java.lang.Integer/3438268394|"
    "1|2|3|4|1|5|5|-300|"
)

GWT_GENERATE_AUTH_TOKEN = (
    "7|0|8|https://cronometer.com/cronometer/|"
    "{gwt_header}|"
    "com.cronometer.shared.rpc.CronometerService|"
    "generateAuthorizationToken|java.lang.String/2004016611|"
    "I|com.cronometer.shared.user.AuthScope/2065601159|"
    "{nonce}|1|2|3|4|4|5|6|6|7|8|{user_id}|3600|7|2|"
)

GWT_FIND_FOODS = (
    "7|0|12|https://cronometer.com/cronometer/|"
    "{gwt_header}|"
    "com.cronometer.shared.rpc.CronometerService|"
    "findFoods|java.lang.String/2004016611|"
    "I|[Lcom.cronometer.shared.foods.FoodSource;/3597302983|"
    "com.cronometer.shared.foods.FoodSearchTabSelection/1776179901|"
    "Z|{nonce}|{query}|"
    "com.cronometer.shared.foods.FoodSource/4236433762|"
    "1|2|3|4|8|5|5|6|7|6|5|8|9|10|11|{max_results}|7|1|12|0|0|0|8|0|0|"
)

GWT_GET_FOOD = (
    "7|0|7|https://cronometer.com/cronometer/|"
    "{gwt_header}|"
    "com.cronometer.shared.rpc.CronometerService|"
    "getFood|java.lang.String/2004016611|"
    "I|{nonce}|"
    "1|2|3|4|2|5|6|7|{food_source_id}|"
)

GWT_UPDATE_DIARY = (
    "7|0|12|https://cronometer.com/cronometer/|"
    "{gwt_header}|"
    "com.cronometer.shared.rpc.CronometerService|"
    "updateDiary|java.lang.String/2004016611|"
    "I|java.util.List|{nonce}|"
    "java.util.Collections$SingletonList/1586180994|"
    "com.cronometer.shared.entries.changes.AddEntryChange/3949104564|"
    "com.cronometer.shared.entries.models.Serving/2553599101|"
    "com.cronometer.shared.entries.models.Day/782579793|"
    "1|2|3|4|3|5|6|7|8|{user_id}|9|10|1|1|11|12|"
    "{day}|{month}|{year}|{quantity}|{diary_group}|0|{measure_id}|0|0|"
    "{weight_grams}|{food_source_id}|A|{food_id}|0|1|"
)

GWT_REMOVE_SERVING = (
    "7|0|8|https://cronometer.com/cronometer/|"
    "{gwt_header}|"
    "com.cronometer.shared.rpc.CronometerService|"
    "removeServing|java.lang.String/2004016611|"
    "J|I|{nonce}|"
    "1|2|3|4|3|5|6|7|8|{serving_id}|{user_id}|"
)



# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CronometerError(Exception):
    pass


class AuthError(CronometerError):
    pass


# ---------------------------------------------------------------------------
# Cronometer client
# ---------------------------------------------------------------------------

class CronometerClient:
    def __init__(
        self,
        email: str,
        password: str,
        gwt_permutation: Optional[str] = None,
        gwt_header: Optional[str] = None,
    ) -> None:
        self.email = email
        self.password = password
        self.gwt_permutation = gwt_permutation or DEFAULT_GWT_PERMUTATION
        self.gwt_header = gwt_header or DEFAULT_GWT_HEADER

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; Claudenometer/1.0)",
        })
        self._nonce: Optional[str] = None
        self._user_id: Optional[str] = None
        self._authenticated = False

        # Session cookie persistence so we don't re-auth on every MCP call.
        data_dir = os.environ.get(
            "CRONOMETER_DATA_DIR",
            str(Path.home() / ".local" / "share" / "claudenometer"),
        )
        self._cookie_path = Path(data_dir) / ".session_cookies"

    # -----------------------------------------------------------------------
    # Authentication steps
    # -----------------------------------------------------------------------

    def _get_anticsrf(self) -> str:
        resp = self._session.get(LOGIN_HTML_URL, timeout=15)
        resp.raise_for_status()
        m = re.search(r'name="anticsrf"\s+value="([^"]+)"', resp.text)
        if not m:
            raise AuthError("Could not find anti-CSRF token on login page")
        return m.group(1)

    def _form_login(self, anticsrf: str) -> None:
        resp = self._session.post(
            LOGIN_API_URL,
            data={
                "anticsrf": anticsrf,
                "username": self.email,
                "password": self.password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        resp.raise_for_status()
        try:
            result = resp.json()
        except Exception:
            raise AuthError(f"Login returned non-JSON response: {resp.text[:200]}")
        if result.get("error"):
            raise AuthError(f"Login failed: {result['error']}")
        if not (result.get("success") or result.get("redirect")):
            raise AuthError(f"Login failed: unexpected response {result}")

        self._nonce = self._session.cookies.get("sesnonce")
        if not self._nonce:
            raise AuthError("Login succeeded but no sesnonce cookie received")

    def _discover_gwt_hashes(self) -> None:
        """Fetch nocache.js for permutation hash, then cache.js for gwt_header."""
        try:
            resp = self._session.get(GWT_NOCACHE_JS_URL, timeout=15)
            resp.raise_for_status()
            perm_m = re.search(r"='([A-F0-9]{32})'", resp.text)
            if not perm_m:
                logger.warning("Could not extract permutation hash; using default")
                return
            permutation = perm_m.group(1)

            cache_url = GWT_CACHE_JS_URL.replace("{permutation}", permutation)
            resp = self._session.get(cache_url, timeout=20)
            resp.raise_for_status()
            header_m = re.search(r"'app','([A-F0-9]{32})'", resp.text)
            if not header_m:
                logger.warning("Could not extract GWT header from cache.js; using default")
                self.gwt_permutation = permutation
                return

            self.gwt_permutation = permutation
            self.gwt_header = header_m.group(1)
            logger.info(
                "GWT hashes: permutation=%s header=%s",
                self.gwt_permutation,
                self.gwt_header,
            )
        except Exception:
            logger.warning("GWT hash discovery failed; using defaults", exc_info=True)

    def _gwt_authenticate(self) -> None:
        body = GWT_AUTHENTICATE.replace("{gwt_header}", self.gwt_header)
        resp = self._session.post(
            GWT_BASE_URL,
            data=body,
            headers={
                "content-type": GWT_CONTENT_TYPE,
                "x-gwt-module-base": GWT_MODULE_BASE,
                "x-gwt-permutation": self.gwt_permutation,
            },
            timeout=20,
        )
        resp.raise_for_status()
        m = re.search(r"OK\[(\d+),", resp.text)
        if not m:
            raise AuthError(
                f"GWT authenticate could not extract user_id. Response: {resp.text[:200]}"
            )
        self._user_id = m.group(1)
        new_nonce = self._session.cookies.get("sesnonce")
        if new_nonce:
            self._nonce = new_nonce

    def _generate_auth_token(self) -> str:
        body = (
            GWT_GENERATE_AUTH_TOKEN
            .replace("{gwt_header}", self.gwt_header)
            .replace("{nonce}", self._nonce or "")
            .replace("{user_id}", self._user_id or "")
        )
        resp = self._session.post(
            GWT_BASE_URL,
            data=body,
            headers={
                "content-type": GWT_CONTENT_TYPE,
                "x-gwt-module-base": GWT_MODULE_BASE,
                "x-gwt-permutation": self.gwt_permutation,
            },
            timeout=20,
        )
        resp.raise_for_status()
        m = re.search(r'"([^"]+)"', resp.text)
        if not m:
            raise CronometerError(
                f"Could not extract auth token. Response: {resp.text[:200]}"
            )
        return m.group(1)

    # -----------------------------------------------------------------------
    # Session persistence
    # -----------------------------------------------------------------------

    def _save_session(self) -> None:
        try:
            self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "cookies": self._session.cookies.get_dict(),
                "nonce": self._nonce,
                "user_id": self._user_id,
                "gwt_permutation": self.gwt_permutation,
                "gwt_header": self.gwt_header,
            }
            self._cookie_path.write_bytes(pickle.dumps(data))
        except Exception:
            logger.debug("Could not save session", exc_info=True)

    def _restore_session(self) -> bool:
        if not self._cookie_path.exists():
            return False
        try:
            data = pickle.loads(self._cookie_path.read_bytes())
            for k, v in data["cookies"].items():
                self._session.cookies.set(k, v)
            self._nonce = data["nonce"]
            self._user_id = data["user_id"]
            self.gwt_permutation = data.get("gwt_permutation", self.gwt_permutation)
            self.gwt_header = data.get("gwt_header", self.gwt_header)
            self._discover_gwt_hashes()
            self._generate_auth_token()  # validates the session
            return True
        except Exception:
            self._cookie_path.unlink(missing_ok=True)
            return False

    # -----------------------------------------------------------------------
    # Public auth interface
    # -----------------------------------------------------------------------

    def login(self) -> str:
        """Full authentication flow. Returns user_id."""
        if self._authenticated:
            return self._user_id  # type: ignore[return-value]
        if self._restore_session():
            self._authenticated = True
            return self._user_id  # type: ignore[return-value]
        self._discover_gwt_hashes()
        anticsrf = self._get_anticsrf()
        self._form_login(anticsrf)
        self._gwt_authenticate()
        self._authenticated = True
        self._save_session()
        return self._user_id  # type: ignore[return-value]

    def refresh(self) -> str:
        """Re-discover GWT hashes and re-authenticate. Returns user_id."""
        self._authenticated = False
        self._nonce = None
        self._user_id = None
        self._session.cookies.clear()
        if self._cookie_path.exists():
            self._cookie_path.unlink(missing_ok=True)
        self._discover_gwt_hashes()
        anticsrf = self._get_anticsrf()
        self._form_login(anticsrf)
        self._gwt_authenticate()
        self._authenticated = True
        self._save_session()
        return self._user_id  # type: ignore[return-value]

    def _ensure_logged_in(self) -> None:
        if not self._authenticated:
            self.login()

    # -----------------------------------------------------------------------
    # GWT-RPC helper
    # -----------------------------------------------------------------------

    def _gwt_post(self, body: str) -> str:
        resp = self._session.post(
            GWT_BASE_URL,
            data=body,
            headers={
                "content-type": GWT_CONTENT_TYPE,
                "x-gwt-module-base": GWT_MODULE_BASE,
                "x-gwt-permutation": self.gwt_permutation,
            },
            timeout=30,
        )
        resp.raise_for_status()
        if not resp.text.startswith("//OK"):
            raise CronometerError(f"GWT-RPC error: {resp.text[:300]}")
        return resp.text

    # -----------------------------------------------------------------------
    # Public API — food search
    # -----------------------------------------------------------------------

    def find_foods(self, query: str, max_results: int = 25) -> list[dict]:
        """
        Search Cronometer's food database.

        Returns a list of dicts:
            {
                "food_id": int,
                "food_source_id": int,
                "name": str,
                "measure_desc": str,   # default serving description
                "score": int,
            }
        """
        self._ensure_logged_in()
        body = (
            GWT_FIND_FOODS
            .replace("{gwt_header}", self.gwt_header)
            .replace("{nonce}", self._nonce or "")
            .replace("{query}", query.upper())
            .replace("{max_results}", str(max_results))
        )
        raw = self._gwt_post(body)
        return self._parse_find_foods(raw)

    @staticmethod
    def _parse_find_foods(raw: str) -> list[dict]:
        closing = ",0,7]"
        if not raw.startswith("//OK[") or not raw.endswith(closing):
            return []

        st_close = len(raw) - len(closing) - 1
        depth, pos, in_string = 1, st_close - 1, False
        while pos >= 0 and depth > 0:
            ch = raw[pos]
            if ch == '"' and (pos == 0 or raw[pos - 1] != "\\"):
                in_string = not in_string
            elif not in_string:
                if ch == "]":
                    depth += 1
                elif ch == "[":
                    depth -= 1
            pos -= 1
        st_open = pos + 1
        string_table: list[str] = json.loads(raw[st_open: st_close + 1])

        searchhit_idx: Optional[int] = None
        for i, entry in enumerate(string_table):
            if not entry.startswith("[") and "SearchHit" in entry:
                searchhit_idx = i + 1
                break
        if searchhit_idx is None:
            return []

        def resolve(ref: int) -> Optional[str]:
            if 1 <= ref <= len(string_table):
                return string_table[ref - 1]
            return None

        data_section = raw[5:st_open].rstrip(",")
        tokens: list[int] = []
        for part in data_section.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                tokens.append(int(part))
            except ValueError:
                tokens.append(-(10 ** 9))

        results: list[dict] = []
        for i, token in enumerate(tokens):
            if token != searchhit_idx or i < 9:
                continue
            name_ref = tokens[i - 7]
            food_id = tokens[i - 6]
            measure_desc_ref = tokens[i - 5]
            food_source_id = tokens[i - 3]
            score = tokens[i - 9]

            name = resolve(name_ref)
            if name is None:
                continue
            if "/" in name and "." in name.split("/")[0]:
                continue  # Java class descriptor, not a food name

            results.append({
                "food_id": food_id,
                "food_source_id": food_source_id,
                "name": name,
                "measure_desc": resolve(measure_desc_ref) or "",
                "score": score,
            })

        return results

    def get_food(self, food_source_id: int) -> dict:
        """
        Get serving size options for a specific food.

        Returns:
            {
                "food_source_id": int,
                "measures": [
                    {"measure_id": int, "description": str, "weight_grams": float},
                    ...
                ]
            }
        """
        self._ensure_logged_in()
        body = (
            GWT_GET_FOOD
            .replace("{gwt_header}", self.gwt_header)
            .replace("{nonce}", self._nonce or "")
            .replace("{food_source_id}", str(food_source_id))
        )
        raw = self._gwt_post(body)
        return self._parse_get_food(raw, food_source_id)

    @staticmethod
    def _parse_get_food(raw: str, food_source_id: int) -> dict:
        result: dict = {"food_source_id": food_source_id, "measures": []}
        closing = ",0,7]"
        if not raw.startswith("//OK[") or not raw.endswith(closing):
            return result

        st_close = len(raw) - len(closing) - 1
        depth, pos, in_str = 1, st_close - 1, False
        while pos >= 0 and depth > 0:
            ch = raw[pos]
            if ch == '"' and (pos == 0 or raw[pos - 1] != "\\"):
                in_str = not in_str
            elif not in_str:
                if ch == "]":
                    depth += 1
                elif ch == "[":
                    depth -= 1
            pos -= 1
        st_open = pos + 1
        string_table: list[str] = json.loads(raw[st_open: st_close + 1])

        def resolve(ref: int) -> Optional[str]:
            if 1 <= ref <= len(string_table):
                return string_table[ref - 1]
            return None

        measure_type_idx: Optional[int] = None
        for i, entry in enumerate(string_table):
            if (not entry.startswith("[") and "Measure/" in entry
                    and "Measure$" not in entry and "Derived" not in entry):
                measure_type_idx = i + 1
                break
        if measure_type_idx is None:
            return result

        data_section = raw[5:st_open].rstrip(",")
        tokens: list = []
        for part in data_section.split(","):
            part = part.strip()
            if not part:
                continue
            if part.startswith('"') and part.endswith('"'):
                tokens.append(part)
                continue
            try:
                tokens.append(float(part) if "." in part else int(part))
            except ValueError:
                tokens.append(None)

        measures = []
        for i, token in enumerate(tokens):
            if token != measure_type_idx or i < 6:
                continue
            measure_id_val = tokens[i - 4]
            if not isinstance(measure_id_val, int):
                continue

            description = ""
            for offset in (6, 7, 8):
                if i < offset:
                    continue
                ref = tokens[i - offset]
                if isinstance(ref, int) and 1 <= ref <= len(string_table):
                    candidate = string_table[ref - 1]
                    if (candidate
                            and not candidate.startswith("com.")
                            and not candidate.startswith("java.")
                            and not candidate.startswith("[")):
                        description = candidate
                        break

            weight_grams = 0.0
            for j in range(i - 7, max(i - 12, -1), -1):
                if isinstance(tokens[j], float):
                    weight_grams = tokens[j]
                    break

            measures.append({
                "measure_id": measure_id_val,
                "description": description,
                "weight_grams": round(weight_grams, 2),
            })

        result["measures"] = measures
        return result

    # -----------------------------------------------------------------------
    # Public API — diary write
    # -----------------------------------------------------------------------

    def add_serving(
        self,
        food_id: int,
        food_source_id: int,
        measure_id: int,
        quantity: float,
        weight_grams: float,
        diary_date: Optional[date] = None,
        diary_group: int = 1,
    ) -> dict:
        """
        Log a food serving to the Cronometer diary.

        Args:
            food_id:        Numeric food identifier from find_foods().
            food_source_id: Source database identifier from find_foods().
            measure_id:     Measure ID from get_food().  Pass 0 to use the
                            universal measure (requires weight_grams).
            quantity:       Number of servings (or weight in grams when using
                            the universal measure).
            weight_grams:   Weight of the serving in grams.
            diary_date:     Date to log against, defaults to today.
            diary_group:    Meal slot — 1=Breakfast, 2=Lunch, 3=Dinner, 4=Snacks.

        Returns dict with serving_id, food_id, food_source_id.
        """
        self._ensure_logged_in()
        if diary_date is None:
            diary_date = date.today()
        if measure_id == 0:
            measure_id = UNIVERSAL_MEASURE_ID

        measure_base = measure_id & 0xFFFF
        encoded_measure = (diary_group << 16) | measure_base

        qty_str = str(int(quantity)) if quantity == int(quantity) else str(quantity)
        wgt_str = str(int(weight_grams)) if weight_grams == int(weight_grams) else str(weight_grams)

        body = (
            GWT_UPDATE_DIARY
            .replace("{gwt_header}", self.gwt_header)
            .replace("{nonce}", self._nonce or "")
            .replace("{user_id}", self._user_id or "")
            .replace("{day}", str(diary_date.day))
            .replace("{month}", str(diary_date.month))
            .replace("{year}", str(diary_date.year))
            .replace("{quantity}", qty_str)
            .replace("{diary_group}", str(diary_group))
            .replace("{measure_id}", str(encoded_measure))
            .replace("{weight_grams}", wgt_str)
            .replace("{food_source_id}", str(food_source_id))
            .replace("{food_id}", str(food_id))
        )
        raw = self._gwt_post(body)

        inner_m = re.search(r"//OK\[(.+),\d+,7\]$", raw, re.DOTALL)
        if not inner_m:
            raise CronometerError(f"Unexpected updateDiary response: {raw[:300]}")
        fields_m = re.match(r"\d+,\d+,(\d+),\"([^\"]+)\",(\d+),", inner_m.group(1))
        if not fields_m:
            raise CronometerError(f"Could not parse updateDiary fields: {inner_m.group(1)[:200]}")

        return {
            "serving_id": fields_m.group(2),
            "food_id": int(fields_m.group(1)),
            "food_source_id": int(fields_m.group(3)),
        }

    def remove_serving(self, serving_id: str) -> bool:
        """Remove a diary entry by its serving_id."""
        self._ensure_logged_in()
        body = (
            GWT_REMOVE_SERVING
            .replace("{gwt_header}", self.gwt_header)
            .replace("{nonce}", self._nonce or "")
            .replace("{user_id}", self._user_id or "")
            .replace("{serving_id}", serving_id)
        )
        raw = self._gwt_post(body)
        if "//OK" not in raw:
            raise CronometerError(f"removeServing unexpected response: {raw[:200]}")
        return True

    def create_custom_food(
        self,
        name: str,
        serving_name: str,
        serving_grams: float,
        energy_kcal: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        fiber_g: float = 0.0,
    ) -> dict:
        """
        Create a custom food in the user's Cronometer account.

        Nutrients are per-serving (for serving_grams weight).
        Returns {"food_id": int, "food_source_id": int, "name": str}.

        NOTE: The exact GWT-RPC payload for saveFood has not been captured
        from live traffic yet.  This method will raise CronometerError with
        the raw server response so the payload can be reverse-engineered.
        To capture it: open Cronometer in Chrome → DevTools → Network tab →
        create a custom food → copy the saveFood request payload and share it.
        """
        self._ensure_logged_in()
        raise CronometerError(
            "create_custom_food is not yet implemented: the saveFood GWT-RPC "
            "payload has not been captured from live Cronometer traffic. "
            "To implement this, open Cronometer in Chrome DevTools → Network → "
            "create any custom food → find the POST to /cronometer/app → "
            "copy the request body and share it."
        )

    # -----------------------------------------------------------------------
    # Public API — diary read (CSV export)
    # -----------------------------------------------------------------------

    def get_food_log(self, diary_date: Optional[date] = None) -> list[dict]:
        """
        Return all diary entries for a given date.

        Each entry: {name, amount, unit, meal, energy_kcal, protein_g, carbs_g, fat_g}
        """
        self._ensure_logged_in()
        if diary_date is None:
            diary_date = date.today()
        token = self._generate_auth_token()
        resp = self._session.get(
            EXPORT_URL,
            params={
                "nonce": token,
                "generate": "servings",
                "start": diary_date.isoformat(),
                "end": diary_date.isoformat(),
            },
            headers={
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return self._parse_servings_csv(resp.text)

    @staticmethod
    def _parse_servings_csv(csv_text: str) -> list[dict]:
        reader = csv.DictReader(io.StringIO(csv_text))
        entries = []
        for row in reader:
            def fval(key: str) -> float:
                for k, v in row.items():
                    if key.lower() in k.lower():
                        try:
                            return float(v)
                        except (ValueError, TypeError):
                            return 0.0
                return 0.0

            entries.append({
                "name": row.get("Food Name", ""),
                "amount": row.get("Amount", ""),
                "unit": row.get("Unit", ""),
                "meal": row.get("Group", ""),
                "energy_kcal": fval("energy"),
                "protein_g": fval("protein"),
                "carbs_g": fval("carb"),
                "fat_g": fval("fat"),
            })
        return entries

    def get_daily_nutrition(self, diary_date: Optional[date] = None) -> dict:
        """
        Return macro totals for a given date.

        Returns:
            {date, energy_kcal, protein_g, carbs_g, fat_g, fiber_g}
        """
        self._ensure_logged_in()
        if diary_date is None:
            diary_date = date.today()
        token = self._generate_auth_token()
        resp = self._session.get(
            EXPORT_URL,
            params={
                "nonce": token,
                "generate": "dailySummary",
                "start": diary_date.isoformat(),
                "end": diary_date.isoformat(),
            },
            headers={
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return self._parse_nutrition_csv(resp.text, diary_date.isoformat())

    @staticmethod
    def _parse_nutrition_csv(csv_text: str, diary_date: str) -> dict:
        empty = {
            "date": diary_date,
            "energy_kcal": 0.0,
            "protein_g": 0.0,
            "carbs_g": 0.0,
            "fat_g": 0.0,
            "fiber_g": 0.0,
        }
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            if row.get("Date") != diary_date:
                continue

            def fval(key: str) -> float:
                for k, v in row.items():
                    if key.lower() in k.lower():
                        try:
                            return float(v)
                        except (ValueError, TypeError):
                            return 0.0
                return 0.0

            return {
                "date": diary_date,
                "energy_kcal": fval("energy"),
                "protein_g": fval("protein"),
                "carbs_g": fval("carb"),
                "fat_g": fval("fat"),
                "fiber_g": fval("fiber"),
            }

        return empty
