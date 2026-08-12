"""
TikTok 都市伝説・ミステリー系アカウント向け 台本自動生成スクリプト

毎日実行され、Claude(Sonnet)がMarkdown形式で台本を3本生成する。
生成結果は
  1. drafts/YYYY-MM-DD/script_N.md としてローカル(リポジトリ内)に保存
  2. Notionデータベースに1本ずつページとして自動登録
される。

必要な環境変数:
  ANTHROPIC_API_KEY    Anthropic APIキー(必須)
  NOTION_API_KEY       Notion Internal Integration Token(必須)
  NOTION_DATABASE_ID   台本を登録するNotionデータベースのID(必須)

必要なライブラリ:
  pip install anthropic requests
"""

import os
import re
import json
import datetime
import pathlib
import requests
import anthropic

MODEL = "claude-sonnet-5"
NUM_SCRIPTS_PER_DAY = 3
TOPICS_FILE = pathlib.Path("scripts/used_topics.json")
DRAFTS_DIR = pathlib.Path("drafts")
NOTION_VERSION = "2022-06-28"
TITLE_PROPERTY_NAME = "Name"  # Notion DB側のタイトル列の名前に合わせて変更

CHECKLIST_ITEMS = [
    "動画尺が60秒以上あるか(Creator Rewards Program対象条件)",
    "字幕(テロップ)を全編に入れたか",
    "投稿時に「AIによって生成されたコンテンツ」ラベルを設定したか",
    "グロテスク・実在の被害者を特定できる表現になっていないか",
    "投稿時間は日本時間21〜23時台を目安に調整したか",
]

SYSTEM_PROMPT = """あなたはTikTok向け都市伝説・ミステリー系動画の台本作家です。
以下のMarkdown形式のみを出力してください。前置き・説明文・```での囲みは一切不要です。1行目は必ず「# タイトル」から始めてください。

# (ここに動画タイトル)

## 冒頭オーバーレイテキスト
(冒頭3秒に表示するテキスト。20文字以内)

## 台本(ナレーション全文)
(400〜500文字程度。改行を入れず1段落で。淡々とした解説調。フック→展開→オチ/クリフハンガーの4パート構成)

## Nano Banana 画像生成プロンプト
1. (シーン1の情景描写。英語)
2. (シーン2の情景描写。英語)
(以降、合計8〜10個)

## 投稿文・ハッシュタグ
(続きが気になる一文＋フォロー誘導のキャプション文)

(ハッシュタグを半角スペース区切りで1行。例: #都市伝説 #ミステリー #怖い話)

ルール:
- ネタは「日本の都市伝説」「海外都市伝説の翻訳紹介」「未解決事件・不思議な話」のいずれかから自由に選ぶ
- 実在の被害者を特定できるほど生々しい表現は避ける
- グロテスクな描写は避け、雰囲気(靄・暗さ・不穏さ)で見せる
- 既に使用済みのネタは絶対に重複させない
- 見出しの文言(## 冒頭オーバーレイテキスト など)は指定のまま変更しない
"""


def load_used_topics() -> list[dict]:
    if TOPICS_FILE.exists():
        return json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
    return []


def save_used_topics(topics: list[dict]) -> None:
    TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOPICS_FILE.write_text(
        json.dumps(topics, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def generate_one_script_markdown(client: anthropic.Anthropic, used_titles: list[str]) -> str:
    recent = used_titles[-30:]
    user_prompt = (
        f"直近で使用済みのネタ一覧(重複禁止): {recent}\n\n"
        "上記と重複しない新しいネタで台本を1本、指定のMarkdown形式で作成してください。"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = resp.content[0].text.strip()
    # まれにコードブロックで囲まれて返ってきた場合の保険(```markdown ... ``` を剥がす)
    text = re.sub(r"^```(markdown)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def append_checklist_markdown(md_text: str) -> str:
    checklist = "\n".join(f"- [ ] {item}" for item in CHECKLIST_ITEMS)
    return f"{md_text}\n\n## 投稿前チェックリスト\n{checklist}\n"


def extract_title(md_text: str) -> str:
    for line in md_text.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return "無題の台本"


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }


def _heading(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _numbered_item(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]
        },
    }


def _todo(text: str, checked: bool = False) -> dict:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "checked": checked,
        },
    }


def markdown_to_notion_blocks(md_text: str) -> list[dict]:
    """このテンプレート専用の簡易Markdown→Notionブロック変換。
    見出し(#, ##)・番号付きリスト(1. )・チェックリスト(- [ ])・
    それ以外の行(段落)、をそれぞれ対応するNotionブロックに変換する。
    """
    blocks = []
    for raw_line in md_text.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            continue  # タイトル行はNotionページのタイトル欄に使うのでブロック化しない
        if line.startswith("## "):
            blocks.append(_heading(line[3:].strip()))
            continue
        m = re.match(r"^\d+\.\s+(.*)", line)
        if m:
            blocks.append(_numbered_item(m.group(1)))
            continue
        m = re.match(r"^-\s*\[\s?\]\s*(.*)", line)
        if m:
            blocks.append(_todo(m.group(1)))
            continue
        blocks.append(_paragraph(line))
    return blocks


def push_to_notion(api_key: str, database_id: str, md_text: str, title: str) -> None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            TITLE_PROPERTY_NAME: {"title": [{"text": {"content": title}}]}
        },
        "children": markdown_to_notion_blocks(md_text),
    }
    try:
        resp = requests.post(
            "https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=15
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        detail = getattr(e, "response", None)
        detail_text = detail.text if detail is not None else str(e)
        print(f"Notionへの登録に失敗しました: {detail_text}")


def main() -> None:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    notion_api_key = os.environ["NOTION_API_KEY"]
    notion_database_id = os.environ["NOTION_DATABASE_ID"]
    client = anthropic.Anthropic(api_key=api_key)

    date_str = datetime.date.today().isoformat()
    out_dir = DRAFTS_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    used_topics = load_used_topics()
    used_titles = [t["title"] for t in used_topics]

    for i in range(1, NUM_SCRIPTS_PER_DAY + 1):
        md_body = generate_one_script_markdown(client, used_titles)
        md_full = append_checklist_markdown(md_body)
        title = extract_title(md_body)

        used_titles.append(title)
        used_topics.append({"title": title, "date": date_str})

        (out_dir / f"script_{i}.md").write_text(md_full, encoding="utf-8")
        push_to_notion(notion_api_key, notion_database_id, md_full, title)

    save_used_topics(used_topics)
    print(f"{NUM_SCRIPTS_PER_DAY}本の台本を {out_dir} に生成し、Notionにも登録しました。")


if __name__ == "__main__":
    main()