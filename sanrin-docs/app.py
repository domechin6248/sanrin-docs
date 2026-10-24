"""山林お引き取り 書類作成ツール

お客様情報・金額・土地を1回入力すると、同意書・案内文書・契約書・請求書を
差し込み済みの Word ファイルとしてまとめて作成します。
"""

from __future__ import annotations

import datetime as dt
import io
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


if "parcels" not in st.session_state:
    st.session_state.parcels = _empty_parcels()
if "editor_version" not in st.session_state:
    st.session_state.editor_version = 0


def _current_parcels() -> pd.DataFrame:
    """表で編集中の内容（なければ保存済みの一覧）。"""
    key = f"parcel_editor_{st.session_state.editor_version}"
    edited = st.session_state.get(f"{key}_value")
    return edited if isinstance(edited, pd.DataFrame) else st.session_state.parcels


def _add_parcels(rows: list[dict]) -> None:
    base = _current_parcels()
    st.session_state.parcels = pd.concat([base, pd.DataFrame(rows)], ignore_index=True)[PARCEL_COLUMNS]
    st.session_state.editor_version += 1


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
c1, c2 = st.columns(2)
receipt_no = c1.text_input("受付番号 *", placeholder="0001")
c1, c2 = st.columns(2)
name = c1.text_input("お名前 *", placeholder="山田 太郎")
furigana = c2.text_input("フリガナ", placeholder="ヤマダ タロウ")
c1, c2 = st.columns([1, 2])
postal = c1.text_input("郵便番号", placeholder="895-0076")
customer_address = c2.text_input("ご住所", placeholder="鹿児島県〇〇市〇〇町1丁目2番3号")
c1, c2 = st.columns(2)
phone = c1.text_input("電話番号", placeholder="090-1234-5678")
email = c2.text_input("メールアドレス", placeholder="example@mail.com")

# --- 金額・日付 ---
st.subheader("💴 金額・日付")
today = dt.datetime.now(JST).date()
c1, c2 = st.columns(2)
with c1:
    amount = st.number_input("お引き取り費用（税込・円） *", min_value=0, step=1000, value=0)
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
