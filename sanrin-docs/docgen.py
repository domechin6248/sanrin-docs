"""テンプレート（.docx）の {{変数}} を差し込んで書類を作る。

テンプレートは Word ファイルそのもので、本文・表・フッターにある {{変数名}} を置き換えます。
土地の表は、1行目を見本にして筆の数だけ行を増やします（テンプレートの行数より少ない場合は空行を残します）。
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

TEMPLATE_DIR = Path(__file__).parent / "templates"

# 作成する書類（テンプレートのファイル名 → 表示名）
DOCUMENTS = {
    "01_同意書.docx": "同意書（兼お申込み書）",
    "02_見積書送付のご案内.docx": "見積書送付のご案内",
    "03_山林引き取り契約書.docx": "山林引き取り契約書",
    "04_契約書送付のご案内.docx": "契約書送付のご案内",
    "05_請求書.docx": "請求書",
}

_ROW_RE = re.compile(r"<w:tr[ >].*?</w:tr>", re.S)
_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
_PARCEL_KEYS = ("土地所在地", "地番", "地目", "地積", "持分")


def _expand_parcel_rows(xml: str, parcel_count: int) -> str:
    """土地の表の行を、筆の数に合わせて増やす。"""
    rows = [m for m in _ROW_RE.finditer(xml) if "{{土地所在地" in m.group(0)]
    if not rows:
        return xml
    proto = rows[0].group(0)
    total = max(parcel_count, len(rows))
    new_rows = []
    for index in range(1, total + 1):
        row = proto
        for key in _PARCEL_KEYS:
            row = row.replace(f"{{{{{key}1}}}}", f"{{{{{key}{index}}}}}")
        # 「No.」欄の番号（見本行の 1）を差し替える
        row = re.sub(r"(<w:t(?: [^>]*)?>)1(</w:t>)", rf"\g<1>{index}\g<2>", row, count=1)
        new_rows.append(row)
    start, end = rows[0].start(), rows[-1].end()
    return xml[:start] + "".join(new_rows) + xml[end:]


def _fill(xml: str, values: dict[str, str]) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        return escape(str(values.get(key, "")))

    return _PLACEHOLDER_RE.sub(repl, xml)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def find_template(template_name: str) -> Path:
    """テンプレートのファイルを探す。

    Mac からアップロードすると「ご」などの濁点が分かれた形（NFD）で保存されることがあるため、
    表記を揃えて比較し、先頭の番号（01_ など）が同じファイルも候補にする。
    """
    exact = TEMPLATE_DIR / template_name
    if exact.exists():
        return exact
    files = [f for f in TEMPLATE_DIR.glob("*.docx")] if TEMPLATE_DIR.exists() else []
    for f in files:
        if _nfc(f.name) == _nfc(template_name):
            return f
    prefix = template_name.split("_", 1)[0] + "_"
    for f in files:
        if f.name.startswith(prefix):
            return f
    found = "、".join(sorted(_nfc(f.name) for f in files)) or "（なし）"
    raise FileNotFoundError(
        f"テンプレート「{template_name}」が templates フォルダに見つかりません。見つかったファイル：{found}"
    )


def render(template_name: str, values: dict[str, str], parcel_count: int) -> bytes:
    """テンプレートに値を差し込んだ .docx をバイト列で返す。"""
    source = find_template(template_name)
    out = io.BytesIO()
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                xml = data.decode("utf-8")
                if "{{" in xml:
                    xml = _expand_parcel_rows(xml, parcel_count)
                    xml = _fill(xml, values)
                data = xml.encode("utf-8")
            zout.writestr(item, data)
    return out.getvalue()


def template_variables() -> dict[str, set[str]]:
    """各テンプレートで使っている変数名（確認用）。"""
    found: dict[str, set[str]] = {}
    for name in DOCUMENTS:
        with zipfile.ZipFile(find_template(name)) as z:
            keys: set[str] = set()
            for item in z.namelist():
                if item.startswith("word/") and item.endswith(".xml"):
                    keys |= set(_PLACEHOLDER_RE.findall(z.read(item).decode("utf-8")))
            found[name] = keys
    return found
