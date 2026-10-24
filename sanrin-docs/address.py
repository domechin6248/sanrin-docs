"""住所（都道府県・市区町村・町名）の一覧と読みがな。

ハザード自動判定ツール（sanrin-tool）と同じデータ源・同じ並べ方です。
- 一覧：Geonex の住所カスケードAPI
- 読み：Geolonia の住所データ（住所ベースレジストリ由来）
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import streamlit as st

USER_AGENT = "sanrin-docs-tool/1.0 (streamlit)"
GEONEX_API_URL = "https://geonex-maps.com/api"
GEOLONIA_TOWN_URL = "https://japanese-addresses-v2.geoloniamaps.com/api/ja"
# ハザード自動判定ツールと同じクライアントキー
GEONEX_API_KEY = "ab5eddf53320d70f0cf0c908d6b69e9179e14296752eb226c699fcbd6fa8874e"

KANA_ROWS = (
    ("あ", "ぁあぃいぅうぇえぉお"),
    ("か", "かがきぎくぐけげこご"),
    ("さ", "さざしじすずせぜそぞ"),
    ("た", "ただちぢっつづてでとど"),
    ("な", "なにぬねの"),
    ("は", "はばぱひびぴふぶぷへべぺほぼぽ"),
    ("ま", "まみむめも"),
    ("や", "ゃやゅゆょよ"),
    ("ら", "らりるれろ"),
    ("わ", "ゎわゐゑをん"),
)
_GOJUON_RANK = {
    char: index for index, char in enumerate("".join(chars for _, chars in KANA_ROWS) + "ー・")
}


def _get_json(url: str, headers: dict | None = None):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError):
        return None


def _geonex(path: str):
    return _get_json(f"{GEONEX_API_URL}{path}", {"X-Geonex-Api-Key": GEONEX_API_KEY})


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def prefectures() -> list[dict]:
    payload = _geonex("/cascade/prefectures")
    if not isinstance(payload, dict):
        return []
    return [i for i in payload.get("prefectures") or [] if i.get("name") and i.get("code")]


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def cities(pref_code: str) -> list[dict]:
    payload = _geonex(f"/cascade/cities?pref_code={pref_code}")
    if not isinstance(payload, dict):
        return []
    return [i for i in payload.get("cities") or [] if i.get("name") and i.get("code")]


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def towns(city_code: str) -> list[str]:
    payload = _geonex(f"/cascade/oaza?city_code={city_code}")
    if not isinstance(payload, dict):
        return []
    names = []
    for item in payload.get("oaza") or []:
        name = (item.get("name") or "").strip()
        if name and name != "（地区なし）":
            names.append(name)
    return names


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def area_readings() -> dict:
    """都道府県と市区町村の読み。"""
    payload = _get_json(f"{GEOLONIA_TOWN_URL}.json")
    prefs: dict[str, str] = {}
    city_map: dict[str, dict[str, str]] = {}
    if not isinstance(payload, dict):
        return {"prefectures": prefs, "cities": city_map}
    for row in payload.get("data") or []:
        pref_name = (row.get("pref") or "").strip()
        pref_kana = (row.get("pref_k") or "").strip()
        if pref_name and pref_kana:
            prefs[pref_name] = pref_kana
        readings = {}
        for city in row.get("cities") or []:
            name = (city.get("city") or "").strip()
            reading = (city.get("city_k") or "").strip()
            ward = (city.get("ward") or "").strip()
            county = (city.get("county") or "").strip()
            if ward:
                name = f"{name}{ward}"
                reading = f"{reading}{(city.get('ward_k') or '').strip()}"
            elif county:
                name = f"{county}{name}"
            if name and reading and name not in readings:
                readings[name] = reading
        if pref_name:
            city_map[pref_name] = readings
    return {"prefectures": prefs, "cities": city_map}


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def town_readings(pref_name: str, city_name: str) -> dict[str, str]:
    path = f"{urllib.parse.quote(pref_name)}/{urllib.parse.quote(city_name)}.json"
    payload = _get_json(f"{GEOLONIA_TOWN_URL}/{path}")
    readings: dict[str, str] = {}
    if not isinstance(payload, dict):
        return readings
    for row in payload.get("data") or []:
        name = (row.get("oaza_cho") or "").strip()
        kana = (row.get("oaza_cho_k") or "").strip()
        if name and kana and name not in readings:
            readings[name] = kana
    return readings


def to_hiragana(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in text)


def kana_row(reading: str) -> str | None:
    hira = to_hiragana(reading)
    if not hira:
        return None
    for label, chars in KANA_ROWS:
        if hira[0] in chars:
            return label
    return None


def _reading_key(reading: str) -> tuple[int, ...]:
    return tuple(_GOJUON_RANK.get(c, 10_000 + ord(c)) for c in to_hiragana(reading))


def _reading(name: str, readings: dict[str, str]) -> str:
    if readings.get(name):
        return readings[name]
    return name if kana_row(name) else ""


def group_by_kana(names: list[str], readings: dict[str, str]) -> dict[str, list[str]]:
    """名前を「あ・か・さ…」の行ごとに、五十音順で分ける。読めないものは「他」。"""
    grouped = {label: [] for label, _ in KANA_ROWS}
    unread, ranked = [], []
    for name in names:
        reading = _reading(name, readings)
        row = kana_row(reading)
        if row is None:
            unread.append(name)
        else:
            ranked.append((_reading_key(reading), name, row))
    ranked.sort()
    for _, name, row in ranked:
        grouped[row].append(name)
    result = {label: grouped[label] for label, _ in KANA_ROWS if grouped[label]}
    if unread:
        result["他"] = sorted(unread)
    return result


def label(name: str, readings: dict[str, str]) -> str:
    reading = to_hiragana(_reading(name, readings))
    if not reading or reading == to_hiragana(name):
        return name
    return f"{name}（{reading}）"


ZIPCLOUD_URL = "https://zipcloud.ibsnet.co.jp/api/search"


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24 * 7)
def lookup_postal(code: str) -> list[str]:
    """郵便番号（7桁）から「都道府県＋市区町村＋町域」の候補を返す（郵便番号検索API zipcloud）。"""
    payload = _get_json(f"{ZIPCLOUD_URL}?zipcode={urllib.parse.quote(code)}")
    if not isinstance(payload, dict):
        return []
    results = []
    for row in payload.get("results") or []:
        text = f"{row.get('address1', '')}{row.get('address2', '')}{row.get('address3', '')}".strip()
        if text and text not in results:
            results.append(text)
    return results
