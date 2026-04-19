"""
Cronometer GWT-RPC client.

Cronometer has no public API; it communicates via GWT-RPC over HTTPS.
This module reverse-engineers that protocol based on the open-source
cronometer-mcp project and GWT-RPC version 7 spec.

Protocol summary:
  - Request: POST /cronometer/cronometer.rpc with a pipe-delimited payload
  - Response: //OK[token1,...,stringN,...,string_count,0,7] or //EX[...]
  - A "permutation hash" (32 hex chars) is baked into each Cronometer JS
    deploy and must appear in both the request body and X-GWT-Permutation
    header. We fetch it dynamically from the nocache.js bootstrap file so
    updates survive Cronometer redeploys without any code changes.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

import requests


CRONOMETER_BASE = "https://cronometer.com/"
CRONOMETER_RPC = "https://cronometer.com/cronometer/cronometer.rpc"
CRONOMETER_NOCACHE = "https://cronometer.com/cronometer/cronometer.nocache.js"
GWT_MODULE_BASE = "https://cronometer.com/cronometer/"
GWT_INTERFACE = "com.cronometer.shared.rpc.CronometerService"


class CronometerError(Exception):
    pass


class AuthError(CronometerError):
    pass


# ---------------------------------------------------------------------------
# GWT-RPC response reader
# ---------------------------------------------------------------------------

class _GWTReader:
    """
    Parses a GWT-RPC v7 //OK[...] response into a typed token stream.

    GWT-RPC response layout:
        //OK[d0,d1,...,dN, s1,s2,...,sM, M, 0, 7]
          ^-- data tokens  ^-- string table  ^--metadata

    Data tokens are read left-to-right in serialization order.
    String tokens are integer references into the string table.
    String table: index 1 = s1 (first string written by the server),
                  index M = sM (last string written).
    """

    def __init__(self, text: str) -> None:
        m = re.match(r"//OK\[(.*)\]$", text.strip(), re.DOTALL)
        if not m:
            raise CronometerError(f"Unexpected GWT response: {text[:200]}")

        raw: list = []
        for tok in re.findall(r'"(?:[^"\\]|\\.)*"|[^,]+', m.group(1)):
            tok = tok.strip()
            if tok.startswith('"') and tok.endswith('"'):
                raw.append(tok[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
            elif tok == "null":
                raw.append(None)
            elif tok == "true":
                raw.append(True)
            elif tok == "false":
                raw.append(False)
            else:
                try:
                    raw.append(int(tok))
                except ValueError:
                    try:
                        raw.append(float(tok))
                    except ValueError:
                        raw.append(tok)

        if len(raw) < 3:
            raise CronometerError("GWT response too short")

        # Metadata
        string_count = int(raw[-3])

        # String table: raw[-(3+M)..-4] in left-to-right order → indices 1..M
        str_start = -(3 + string_count) if string_count > 0 else len(raw)
        str_entries = raw[str_start:-3] if string_count > 0 else []
        self._strings: dict[int, str] = {
            i + 1: str_entries[i] for i in range(len(str_entries))
        }

        self._data = raw[: str_start if string_count > 0 else -3]
        self._pos = 0

    # ---- primitive reads (no string resolution) ----------------------------

    def read_int(self) -> int:
        val = self._data[self._pos]
        self._pos += 1
        return int(val)

    def read_float(self) -> float:
        val = self._data[self._pos]
        self._pos += 1
        return float(val)

    def read_bool(self) -> bool:
        val = self._data[self._pos]
        self._pos += 1
        if isinstance(val, bool):
            return val
        return bool(int(val))

    # ---- string read (resolves reference) -----------------------------------

    def read_string(self) -> Optional[str]:
        val = self._data[self._pos]
        self._pos += 1
        if val is None:
            return None
        if isinstance(val, int) and val > 0:
            return self._strings.get(val)
        return str(val)

    # ---- generic read -------------------------------------------------------

    def read(self):
        val = self._data[self._pos]
        self._pos += 1
        if isinstance(val, int) and val > 0 and val in self._strings:
            return self._strings[val]
        return val

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos


# ---------------------------------------------------------------------------
# Cronometer client
# ---------------------------------------------------------------------------

class CronometerClient:
    def __init__(self, email: str, password: str, tz_offset: int = 0) -> None:
        self.email = email
        self.password = password
        self.tz_offset = tz_offset

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; Claudenometer/1.0)",
        })
        self._perm_hash: Optional[str] = None
        self._user_id: Optional[int] = None

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _fetch_perm_hash(self) -> str:
        """
        Fetch the GWT permutation hash dynamically from Cronometer's
        nocache.js bootstrap file.  The hash is the 32-character hex prefix
        of each .cache.js filename and must match the running JS bundle.
        Fetching it fresh on every (re)login means we survive Cronometer
        redeploys without any code changes.
        """
        r = self._session.get(CRONOMETER_NOCACHE, timeout=15)
        r.raise_for_status()
        # Try progressively looser patterns to survive Cronometer JS bundle changes
        for pattern in [
            r"'([0-9A-F]{32})\.cache\.js'",   # original: uppercase, single quotes
            r'"([0-9A-F]{32})\.cache\.js"',    # uppercase, double quotes
            r"'([0-9a-f]{32})\.cache\.js'",    # lowercase, single quotes
            r'"([0-9a-f]{32})\.cache\.js"',    # lowercase, double quotes
            r"([0-9A-Fa-f]{32})\.cache\.js",   # any case, no quotes
        ]:
            hashes = re.findall(pattern, r.text)
            if hashes:
                return hashes[0]
        raise CronometerError(
            "Could not find GWT permutation hash in cronometer.nocache.js. "
            f"File starts with: {r.text[:300]!r}"
        )

    def _build_payload(self, method: str, params: list) -> str:
        """
        Build a GWT-RPC v7 request payload.

        Format:
            7|0|STRING_COUNT|S1|S2|...|SN|1|2|3|4|PARAM_COUNT|P1|P2|...|PN|

        The first four strings are always the four fixed fields (module base,
        permutation hash, service interface, method name).  String params are
        appended to the string table and referenced by 1-based index.
        Primitive params (int, float, bool) are written inline.
        """
        strings = [GWT_MODULE_BASE, self._perm_hash, GWT_INTERFACE, method]
        param_refs: list[str] = []
        for p in params:
            if isinstance(p, str):
                strings.append(p)
                param_refs.append(str(len(strings)))
            elif isinstance(p, bool):
                param_refs.append("1" if p else "0")
            else:
                param_refs.append(str(p))

        parts = ["7", "0", str(len(strings))]
        parts.extend(strings)
        parts += ["1", "2", "3", "4", str(len(params))]
        parts.extend(param_refs)
        return "|".join(parts) + "|"

    def _rpc(self, method: str, params: list) -> _GWTReader:
        payload = self._build_payload(method, params)
        r = self._session.post(
            CRONOMETER_RPC,
            data=payload.encode("utf-8"),
            headers={
                "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
                "X-GWT-Module-Base": GWT_MODULE_BASE,
                "X-GWT-Permutation": self._perm_hash,
            },
            timeout=30,
        )
        r.raise_for_status()
        if r.text.startswith("//EX"):
            raise CronometerError(f"GWT RPC exception: {r.text[:300]}")
        return _GWTReader(r.text)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def login(self) -> int:
        """Authenticate and return the Cronometer userId."""
        self._perm_hash = self._fetch_perm_hash()
        reader = self._rpc("authenticate", [self.email, self.password, self.tz_offset])
        # authenticate() returns a User object.  The first field is userId (int).
        if reader.remaining == 0:
            raise AuthError("Empty response from authenticate — bad credentials?")
        try:
            user_id = reader.read_int()
        except (IndexError, ValueError) as exc:
            raise AuthError(f"Could not parse userId from authenticate response: {exc}")
        if user_id <= 0:
            raise AuthError(f"Login failed — userId={user_id}")
        self._user_id = user_id
        return user_id

    def refresh(self) -> int:
        """Re-fetch the permutation hash and re-authenticate in one shot."""
        self._perm_hash = None
        self._user_id = None
        self._session.cookies.clear()
        return self.login()

    def _ensure_logged_in(self) -> None:
        if self._user_id is None:
            self.login()

    def search_foods(self, query: str, max_results: int = 25) -> list[dict]:
        """
        Search Cronometer's food database.

        Returns a list of dicts:
            {
                "food_source_id": str,   # e.g. "1:12345" or "CUSTOM:67890"
                "name": str,
                "servings": [
                    {"serving_id": int, "measure": str, "grams": float},
                    ...
                ]
            }
        """
        self._ensure_logged_in()
        reader = self._rpc("searchFoods", [query, max_results])
        return self._parse_food_search(reader)

    def _parse_food_search(self, reader: _GWTReader) -> list[dict]:
        """
        Parse searchFoods GWT response.

        Cronometer serializes a List<FoodSource> where each FoodSource has:
          - int foodId
          - String foodSourceId  (e.g. "1:12345")
          - String description
          - List<Serving> servings

        Each Serving:
          - int servingId
          - String measure  (e.g. "1 cup")
          - double grams

        GWT serializes lists as: count, then each element's fields.
        """
        results: list[dict] = []
        if reader.remaining == 0:
            return results

        try:
            count = reader.read_int()
            for _ in range(count):
                food_id = reader.read_int()
                food_source_id = reader.read_string() or str(food_id)
                name = reader.read_string() or "Unknown"

                serving_count = reader.read_int()
                servings = []
                for _ in range(serving_count):
                    s_id = reader.read_int()
                    measure = reader.read_string() or "serving"
                    grams = reader.read_float()
                    servings.append({"serving_id": s_id, "measure": measure, "grams": grams})

                results.append({
                    "food_source_id": food_source_id,
                    "name": name,
                    "servings": servings,
                })
        except (IndexError, ValueError, TypeError):
            # Partial parse is better than nothing; return what we have
            pass

        return results

    def add_serving(
        self,
        food_source_id: str,
        serving_id: int,
        amount: float,
        diary_date: Optional[str] = None,
    ) -> bool:
        """
        Log a food serving to the Cronometer diary.

        Args:
            food_source_id: Cronometer food ID (e.g. "1:12345").
            serving_id:     Serving type ID from search_foods().
            amount:         Number of servings (e.g. 1.5).
            diary_date:     "YYYY-MM-DD", defaults to today.

        Returns True on success; raises CronometerError on failure.
        """
        self._ensure_logged_in()
        if diary_date is None:
            diary_date = date.today().isoformat()

        self._rpc(
            "addServingToDay",
            [diary_date, food_source_id, serving_id, amount, self._user_id],
        )
        return True

    def get_food_log(self, diary_date: Optional[str] = None) -> list[dict]:
        """
        Return all logged diary entries for a given date.

        Each entry dict:
            {"name": str, "amount": float, "measure": str, "energy_kcal": float}
        """
        self._ensure_logged_in()
        if diary_date is None:
            diary_date = date.today().isoformat()

        reader = self._rpc("getDiaryEntries", [diary_date, self._user_id])
        return self._parse_diary(reader)

    def _parse_diary(self, reader: _GWTReader) -> list[dict]:
        entries: list[dict] = []
        if reader.remaining == 0:
            return entries
        try:
            count = reader.read_int()
            for _ in range(count):
                name = reader.read_string() or "Unknown"
                amount = reader.read_float()
                measure = reader.read_string() or "serving"
                energy = reader.read_float()
                entries.append({
                    "name": name,
                    "amount": amount,
                    "measure": measure,
                    "energy_kcal": energy,
                })
        except (IndexError, ValueError, TypeError):
            pass
        return entries

    def get_daily_nutrition(self, diary_date: Optional[str] = None) -> dict:
        """
        Return macro totals for a given date using Cronometer's CSV export.

        Returns:
            {
                "date": str,
                "energy_kcal": float,
                "protein_g": float,
                "carbs_g": float,
                "fat_g": float,
                "fiber_g": float,
            }
        """
        self._ensure_logged_in()
        if diary_date is None:
            diary_date = date.today().isoformat()

        r = self._session.get(
            f"{CRONOMETER_BASE}export",
            params={
                "format": "csv",
                "generate": "dailySummary",
                "start": diary_date,
                "end": diary_date,
            },
            timeout=20,
        )
        r.raise_for_status()
        return self._parse_nutrition_csv(r.text, diary_date)

    def _parse_nutrition_csv(self, csv_text: str, diary_date: str) -> dict:
        empty = {
            "date": diary_date,
            "energy_kcal": 0.0,
            "protein_g": 0.0,
            "carbs_g": 0.0,
            "fat_g": 0.0,
            "fiber_g": 0.0,
        }
        lines = [l for l in csv_text.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            return empty

        headers = [h.strip().strip('"') for h in lines[0].split(",")]

        for line in lines[1:]:
            values = [v.strip().strip('"') for v in line.split(",")]
            if not values or values[0] != diary_date:
                continue
            row = dict(zip(headers, values))

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
