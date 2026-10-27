"""山林お引き取り 書類作成ツール

お客様情報・金額・土地を1回入力すると、同意書・案内文書・契約書・請求書を
差し込み済みの Word ファイルとしてまとめて作成します。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import urllib.error
import urllib.request
import zipfile

import pandas as pd
import streamlit as st

import address
import docgen

st.set_page_config(page_title="書類作成ツール", page_icon="📄", layout="centered")

JST = dt.timezone(dt.timedelta(hours=9))
CHIMOKU = ["山林", "保安林", "原野", "雑種地", "宅地", "公衆用道路", "池沼", "田", "畑", "その他"]
FARMLAND = {"田", "畑"}
PARCEL_COLUMNS = ["土地所在地", "地番", "地目", "地積", "持分"]


def _secret(key: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(key, default))
    except Exception:  # secrets.toml が無い場合
        return default


def reiwa(d: dt.date) -> tuple[str, str, str]:
    """日付を令和の（年, 月, 日）の文字列にする。"""
    return str(d.year - 2018), str(d.month), str(d.day)


def reiwa_text(d: dt.date) -> str:
    y, m, day = reiwa(d)
    return f"令和{y}年{m}月{day}日"


def _empty_parcels() -> pd.DataFrame:
    return pd.DataFrame(columns=PARCEL_COLUMNS)


def _query_fee() -> int:
    """ハザード自動判定ツールから渡された引き取り費用。"""
    raw = str(st.query_params.get("fee", "") or "").replace(",", "").strip()
    raw = raw.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _query_lands() -> list[dict]:
    """見積の対象地。land=所在|地番|地積 を繰り返す。"""
    rows = []
    for value in st.query_params.get_all("land"):
        parts = str(value).split("|")
        location = parts[0].strip() if parts else ""
        chiban = parts[1].strip() if len(parts) > 1 else ""
        area = parts[2].strip() if len(parts) > 2 else ""
        if not location and not chiban:
            continue
        rows.append(
            {
                "土地所在地": location,
                "地番": chiban,
                "地目": "山林",
                "地積": area,
                "持分": "全部",
            }
        )
    return rows


def _apply_quote_query() -> None:
    fee = _query_fee()
    if fee and st.session_state.get("_fee_applied") != fee:
        st.session_state.pickup_fee = fee
        st.session_state._fee_applied = fee
    elif "pickup_fee" not in st.session_state:
        st.session_state.pickup_fee = 0

    lands = _query_lands()
    signature = tuple((row["土地所在地"], row["地番"], row["地積"]) for row in lands)
    if lands and st.session_state.get("_lands_applied") != signature:
        st.session_state.parcels = pd.DataFrame(lands)[PARCEL_COLUMNS]
        st.session_state.editor_version += 1
        st.session_state._lands_applied = signature


if "parcels" not in st.session_state:
    st.session_state.parcels = _empty_parcels()
if "editor_version" not in st.session_state:
    st.session_state.editor_version = 0
_apply_quote_query()


def _current_parcels() -> pd.DataFrame:
    """表で編集中の内容（なければ保存済みの一覧）。"""
    key = f"parcel_editor_{st.session_state.editor_version}"
    edited = st.session_state.get(f"{key}_value")
    return edited if isinstance(edited, pd.DataFrame) else st.session_state.parcels


def _add_parcels(rows: list[dict]) -> None:
    base = _current_parcels()
    st.session_state.parcels = pd.concat([base, pd.DataFrame(rows)], ignore_index=True)[PARCEL_COLUMNS]
    st.session_state.editor_version += 1


# シート1だけをウェブ公開したCSV。設定シートは公開していない。
_CUSTOMER_SHEET_CSV = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vS9o-2jbZJWMWuP12v7NwhMohaucgYwJdEaFi1YpttAmXf8x6eYS5wNxACxvpOGcL-JYKUYydJb2Xk9/"
    "pub?gid=0&single=true&output=csv"
)
_CUSTOMER_SHEET_EDIT = (
    "https://docs.google.com/spreadsheets/d/1rWmzEHz3iDmMm8oNAVUew0o8OvEs8-uwaNfcaMh-O-0/edit?gid=0"
)


def _sheet_cell(row: dict, *names: str) -> str:
    for name in names:
        value = str(row.get(name, "") or "").strip()
        if value and value.lower() != "nan":
            return value
    return ""


def _format_postal(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    digits = digits.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if len(digits) == 7:
        return f"{digits[:3]}-{digits[3:]}"
    return value.strip()


def _join_address(base: str, detail: str) -> str:
    base, detail = base.strip(), detail.strip()
    if not detail or detail in base:
        return base
    return f"{base}{detail}" if base else detail


def _customers_from_csv(text: str) -> list[dict]:
    """申し込みシートの1行を、書類のお客様欄に入る形にする。"""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    customers = []
    used_labels: set[str] = set()
    for index, row in enumerate(rows, start=1):
        number = _sheet_cell(row, "お客様番号", "受付番号")
        caller = _sheet_cell(row, "発信者のお名前", "発信者")
        owner = _sheet_cell(row, "山林の名義", "名義")
        name = owner or caller
        furigana = _sheet_cell(row, "フリガナ", "お名前フリガナ", "発信者のフリガナ")
        postal = _format_postal(_sheet_cell(row, "郵便番号"))
        address = _join_address(
            _sheet_cell(row, "住所", "ご住所"),
            _sheet_cell(row, "番地・建物名・部屋番号", "番地"),
        )
        phone = _sheet_cell(row, "お電話番号", "電話番号") or _sheet_cell(row, "SMS用携帯番号")
        email = _sheet_cell(row, "メールアドレス", "メール", "Eメール")
        if not any((number, caller, owner, postal, address, phone, email)):
            continue
        label = f"{number}　{name}".strip("　") if number and name else (number or name or f"{index}行目")
        if owner and caller and owner != caller:
            label = f"{label}（発信 {caller}）"
        status = _sheet_cell(row, "進捗状況")
        if status:
            label = f"{label}　{status}"
        unique = label
        suffix = 2
        while unique in used_labels:
            unique = f"{label}（{suffix}）"
            suffix += 1
        used_labels.add(unique)
        customers.append(
            {
                "label": unique,
                "receipt_no": number,
                "name": name,
                "furigana": furigana,
                "postal": postal,
                "address": address,
                "phone": phone,
                "email": email,
            }
        )
    return customers


@st.cache_data(ttl=60, show_spinner=False)
def _load_customers() -> list[dict]:
    request = urllib.request.Request(
        _CUSTOMER_SHEET_CSV, headers={"User-Agent": "sanrin-docs/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8-sig")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("お客様の一覧を読み込めませんでした。") from exc
    first = text.splitlines()[0] if text.splitlines() else ""
    if "お客様番号" not in first and "受付番号" not in first:
        raise RuntimeError("お客様の一覧を読み込めませんでした。")
    return _customers_from_csv(text)


def _apply_customer(row: dict) -> None:
    """選んだ行を入力欄へ入れる。郵便番号の自動補完で番地を消さない。"""
    st.session_state.receipt_no = row["receipt_no"]
    st.session_state.customer_name = row["name"]
    st.session_state.customer_furigana = row["furigana"]
    st.session_state.postal = row["postal"]
    st.session_state.customer_address = row["address"]
    st.session_state.customer_phone = row["phone"]
    st.session_state.customer_email = row["email"]
    st.session_state.postal_autofill = ""
    st.session_state.postal_message = ""


def _fill_address_from_postal() -> None:
    """郵便番号から住所（町名まで）を調べて、ご住所の欄に入れる。"""
    code = "".join(ch for ch in st.session_state.get("postal", "") if ch.isdigit())
    code = code.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    st.session_state.postal_message = ""
    if len(code) != 7:
        if code:
            st.session_state.postal_message = "郵便番号は7桁で入力してください。"
        return
    st.session_state.postal = f"{code[:3]}-{code[3:]}"
    found = address.lookup_postal(code)
    if not found:
        st.session_state.postal_message = "この郵便番号の住所が見つかりませんでした。直接入力してください。"
        return
    current = st.session_state.get("customer_address", "").strip()
    previous = st.session_state.get("postal_autofill", "")
    # まだ空欄か、前回自動で入れたままの場合だけ上書きする（書き足した番地は消さない）
    if not current or current == previous:
        st.session_state.customer_address = found[0]
        st.session_state.postal_autofill = found[0]
        st.session_state.postal_message = (
            "住所を自動で入れました。番地などを書き足してください。"
            if len(found) == 1 else
            f"候補が{len(found)}件あります（{'／'.join(found)}）。最初の候補を入れました。"
        )
    else:
        st.session_state.postal_message = f"郵便番号の住所：{'／'.join(found)}（入力済みのご住所はそのままにしています）"


# ─────────────────────────────── 画面 ───────────────────────────────
st.title("📄 山林お引き取り 書類作成ツール")
st.write("お客様情報・金額・土地を入力すると、各書類を差し込み済みの Word ファイルで作成します。")

with st.expander("⚙️ 会社設定", expanded=not _secret("LINE_ID")):
    line_id = st.text_input(
        "公式LINE ID",
        value=_secret("LINE_ID"),
        placeholder="@sanrin-partners",
        help="毎回入力しなくて済むように、Streamlit の Secrets に LINE_ID として登録できます。",
    )

# --- お客様情報 ---
st.subheader("👤 お客様情報")
pick_col, refresh_col = st.columns([5, 1])
with refresh_col:
    if st.button("更新", help="スプレッドシートの最新の行を読み込みます。"):
        _load_customers.clear()
        st.rerun()
customers: list[dict] = []
customer_error = ""
try:
    customers = _load_customers()
except RuntimeError:
    customer_error = "お客様の一覧を読み込めませんでした。欄へ直接入力してください。"
with pick_col:
    if customer_error:
        st.warning(customer_error)
    elif not customers:
        st.caption("スプレッドシートにまだお客様がありません。シート1に行を追加すると、ここに名前が出ます。")
    else:
        chosen = st.pills(
            "お客様を選ぶ",
            [row["label"] for row in customers],
            selection_mode="single",
            key="customer_pick",
        )
        if isinstance(chosen, list):
            chosen = chosen[0] if chosen else None
        picked = next((row for row in customers if row["label"] == chosen), None)
        if picked and st.session_state.get("_customer_applied") != picked["label"]:
            _apply_customer(picked)
            st.session_state._customer_applied = picked["label"]
        st.caption("名前を押すと、下の欄に入ります。入ったあとに手直しできます。")
st.caption(f"一覧の元データ：[申し込み受付状況]({_CUSTOMER_SHEET_EDIT})")
c1, c2 = st.columns(2)
receipt_no = c1.text_input("受付番号 *", placeholder="0001", key="receipt_no")
c1, c2 = st.columns(2)
name = c1.text_input("お名前 *", placeholder="山田 太郎", key="customer_name")
furigana = c2.text_input("フリガナ", placeholder="ヤマダ タロウ", key="customer_furigana")
c1, c2 = st.columns([1, 2])
postal = c1.text_input(
    "郵便番号", placeholder="895-0076", key="postal", on_change=_fill_address_from_postal,
    help="7桁を入れてEnterを押すと、ご住所の欄に町名まで自動で入ります。",
)
customer_address = c2.text_input(
    "ご住所", placeholder="鹿児島県〇〇市〇〇町1丁目2番3号", key="customer_address",
    help="郵便番号から入った町名の後ろに、番地などを書き足してください。",
)
if st.session_state.get("postal_message"):
    c2.caption(st.session_state.postal_message)
c1, c2 = st.columns(2)
phone = c1.text_input("電話番号", placeholder="090-1234-5678", key="customer_phone")
email = c2.text_input("メールアドレス", placeholder="example@mail.com", key="customer_email")

# --- 金額・日付 ---
st.subheader("💴 金額・日付")
today = dt.datetime.now(JST).date()
c1, c2 = st.columns(2)
with c1:
    amount = st.number_input(
        "お引き取り費用（税込・円） *", min_value=0, step=1000, key="pickup_fee",
    )
    if st.session_state.get("_fee_applied"):
        st.caption("ハザード自動判定ツールの見積合計を入れています。金額はここで直せます。")
    created_on = st.date_input("案内文書の作成日", value=today, format="YYYY/MM/DD")
    invoice_no = st.text_input("請求番号", placeholder="空欄なら受付番号を使います")
with c2:
    st.metric("表示される金額", f"{int(amount):,} 円")
    invoice_on = st.date_input("請求日", value=today, format="YYYY/MM/DD")
    due_on = st.date_input(
        "お支払期限", value=invoice_on + dt.timedelta(days=30), format="YYYY/MM/DD",
        help="請求書に印字されます。",
    )
remarks = st.text_input("請求書の備考", placeholder="（任意）")

# --- 対象の土地 ---
st.subheader("🌲 対象の土地")
mode = st.radio("所在の入れ方", ["リストから選ぶ", "自由に入力する"], horizontal=True)

location = ""
if mode == "リストから選ぶ":
    prefs = address.prefectures()
    if not prefs:
        st.error("都道府県の一覧を取得できませんでした。「自由に入力する」に切り替えてください。")
    else:
        readings = address.area_readings()
        pref_names = [p["name"] for p in prefs]
        pref_reads = readings["prefectures"]
        pref_groups = address.group_by_kana(pref_names, pref_reads)
        want_pref = "鹿児島県"
        want_pref_row = next((r for r, n in pref_groups.items() if want_pref in n), next(iter(pref_groups)))
        pref_row = st.pills("頭文字", list(pref_groups), default=want_pref_row, required=True, key="pref_kana")
        pref_row = pref_row if pref_row in pref_groups else want_pref_row
        shown = pref_groups[pref_row]
        pref_name = st.selectbox(
            "都道府県", shown, index=shown.index(want_pref) if want_pref in shown else 0,
            format_func=lambda n: address.label(n, pref_reads), key=f"pref_{pref_row}",
        )
        pref_code = next(p["code"] for p in prefs if p["name"] == pref_name)

        city_list = address.cities(pref_code)
        if not city_list:
            st.error("市区町村の一覧を取得できませんでした。")
        else:
            city_names = [c["name"] for c in city_list]
            city_reads = readings["cities"].get(pref_name, {})
            city_groups = address.group_by_kana(city_names, city_reads)
            want_city = "薩摩川内市" if pref_name == "鹿児島県" else city_names[0]
            want_city_row = next((r for r, n in city_groups.items() if want_city in n), next(iter(city_groups)))
            city_row = st.pills("頭文字", list(city_groups), default=want_city_row, required=True,
                                key=f"city_kana_{pref_code}")
            city_row = city_row if city_row in city_groups else want_city_row
            shown = city_groups[city_row]
            city_name = st.selectbox(
                "市区町村", shown, index=shown.index(want_city) if want_city in shown else 0,
                format_func=lambda n: address.label(n, city_reads), key=f"city_{pref_code}_{city_row}",
            )
            city_code = next(c["code"] for c in city_list if c["name"] == city_name)

            town_names = address.towns(city_code)
            if not town_names:
                st.error("町名の一覧を取得できませんでした。")
            else:
                town_reads = address.town_readings(pref_name, city_name)
                town_groups = address.group_by_kana(town_names, town_reads)
                town_row = st.pills("頭文字", list(town_groups), default=next(iter(town_groups)),
                                    required=True, key=f"town_kana_{city_code}")
                town_row = town_row if town_row in town_groups else next(iter(town_groups))
                town_name = st.selectbox(
                    "町名", town_groups[town_row],
                    format_func=lambda n: address.label(n, town_reads), key=f"town_{city_code}_{town_row}",
                )
                location = f"{pref_name}{city_name}{town_name}"
else:
    location = st.text_input("所在（都道府県から町名・字まで）", placeholder="鹿児島県薩摩川内市平佐町字〇〇")

with st.form("add_parcel", clear_on_submit=True, border=True):
    if mode == "リストから選ぶ":
        aza = st.text_input("字（あざ）", placeholder="（任意）例：〇〇 → 「字〇〇」として付きます")
    else:
        aza = ""
    c1, c2, c3, c4 = st.columns([1.3, 1, 1, 1])
    with c1:
        chiban = st.text_input("地番 *", placeholder="2180, 2181", help="複数の地番はカンマかスペースで区切れます。")
    with c2:
        chimoku = st.selectbox("地目", CHIMOKU)
    with c3:
        chiseki = st.text_input("地積（㎡）", placeholder="1234.56")
    with c4:
        share = st.text_input("甲の持分", value="全部", help="単独所有は「全部」、共有は「2分の1」など。")
    added = st.form_submit_button("この土地を一覧に追加", type="primary", width="stretch")

if added:
    numbers = [n for n in chiban.replace("、", ",").replace(",", " ").split() if n]
    full_location = location + (f"字{aza.removeprefix('字')}" if aza.strip() else "")
    if not location.strip():
        st.warning("所在を選ぶか入力してから追加してください。")
    elif not numbers:
        st.warning("地番を入力してから追加してください。")
    else:
        _add_parcels([
            {"土地所在地": full_location, "地番": n, "地目": chimoku,
             "地積": chiseki if len(numbers) == 1 else "", "持分": share or "全部"}
            for n in numbers
        ])
        if len(numbers) > 1:
            st.info("地番を複数追加しました。地積は下の一覧で1筆ずつ入力してください。")

st.caption(f"選択中の所在：{location or '（未選択）'}")
if st.session_state.get("_lands_applied"):
    st.caption("見積の対象地を一覧に入れています。地目が違う筆は、表の中で直してください。")
editor_key = f"parcel_editor_{st.session_state.editor_version}"
edited = st.data_editor(
    st.session_state.parcels,
    key=editor_key,
    num_rows="dynamic",
    width="stretch",
    hide_index=True,
    column_config={
        "土地所在地": st.column_config.TextColumn("所在", width="large"),
        "地番": st.column_config.TextColumn("地番"),
        "地目": st.column_config.SelectboxColumn("地目", options=CHIMOKU),
        "地積": st.column_config.TextColumn("地積（㎡）"),
        "持分": st.column_config.TextColumn("甲の持分"),
    },
)
st.session_state[f"{editor_key}_value"] = edited
parcels = edited.fillna("").astype(str)
parcels = parcels[(parcels["土地所在地"].str.strip() != "") | (parcels["地番"].str.strip() != "")]
st.caption("一覧の文字は直接書き換えられます。行を消すときは、行の左端を選んで削除してください。")
if len(parcels) and st.button("一覧を空にする"):
    st.session_state.parcels = _empty_parcels()
    st.session_state.editor_version += 1
    st.rerun()

farm = parcels[parcels["地目"].isin(FARMLAND)]
if len(farm):
    st.warning("農地（田・畑）が含まれています。農地はお引き取りの対象外です（契約書 第1条第3項）。")

# ─────────────────────────────── 書類作成 ───────────────────────────────
st.subheader("🖨️ 書類を作成")
selected = [
    file for file, title in docgen.DOCUMENTS.items()
    if st.checkbox(title, value=True, key=f"doc_{file}")
]

missing = [label for label, value in (("受付番号", receipt_no), ("お名前", name)) if not value.strip()]
if amount <= 0:
    missing.append("お引き取り費用")
if parcels.empty:
    missing.append("対象の土地")
if missing:
    st.info("未入力の項目があります：" + "、".join(missing) + "（空欄のまま作成することもできます）")

if st.button("📄 書類を作成する", type="primary", disabled=not selected):
    cy, cm, cd = reiwa(created_on)
    iy, im, id_ = reiwa(invoice_on)
    values = {
        "受付番号": receipt_no.strip(),
        "氏名": name.strip(),
        "氏名フリガナ": furigana.strip(),
        "郵便番号": postal.strip(),
        "住所": customer_address.strip(),
        "電話番号": phone.strip(),
        "メールアドレス": email.strip(),
        "見積金額": f"{int(amount):,}" if amount else "",
        "作成年": cy, "作成月": cm, "作成日": cd,
        "請求年": iy, "請求月": im, "請求日": id_,
        "請求番号": (invoice_no or receipt_no).strip(),
        "支払期限": reiwa_text(due_on),
        "備考": remarks.strip(),
        "公式LINE_ID": line_id.strip(),
    }
    for i, row in enumerate(parcels.to_dict("records"), start=1):
        for key in docgen._PARCEL_KEYS:
            values[f"{key}{i}"] = str(row.get(key, "")).strip()

    prefix = "_".join(x for x in (receipt_no.strip(), name.strip().replace(" ", "").replace("　", "")) if x)
    files = {}
    for file in selected:
        out_name = f"{prefix}_{file[3:]}" if prefix else file[3:]
        files[out_name] = docgen.render(file, values, len(parcels))
    st.session_state.generated = files

files = st.session_state.get("generated")
if files:
    st.success(f"{len(files)}件の書類を作成しました。")
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, data in files.items():
            z.writestr(fname, data)
    st.download_button("⬇️ すべてまとめてダウンロード（ZIP）", bundle.getvalue(),
                       file_name=f"{receipt_no or '書類'}_一式.zip", mime="application/zip",
                       type="primary", width="stretch")
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    for fname, data in files.items():
        st.download_button(f"⬇️ {fname}", data, file_name=fname, mime=docx_mime, key=f"dl_{fname}")
