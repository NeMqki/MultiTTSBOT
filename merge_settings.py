"""
merge_settings.py - BOT1 と BOT2 のユーザー設定・辞書を1つにまとめる（1回だけ使う道具）

使い方（BOTを2つとも止めてから実行）:
  python merge_settings.py <BOT1のデータフォルダ> <BOT2のデータフォルダ> <まとめた結果の出力先フォルダ>

例:
  python merge_settings.py . data_bot2 shared

  → shared フォルダに user_settings.json と dictionary.json ができます。
    両方のBOTの config.txt で  shared_dir = shared  と指定すると、共有になります。

まとめ方:
  ユーザー設定（user_settings.json）
    同じ人が両方にいるときは、次の順で「より確かな設定」のほうを残します。
      1. 本人が /voice で選んだ声
      2. 自動で割り当てたランダムの声
      3. 旧形式（番号）の設定、および旧版が自動で入れていた「VOICEROID2の0番」を移行で固定しただけのもの
         （user_settings.json.bak-before-migration が残っていれば、それを見て判断します）
    同じ順位どうしなら、BOT1 の設定を残します（速度・ピッチも一緒に）。
  辞書（dictionary.json）
    サーバーごとに、両方の登録をまとめます。同じ語で読みが違うときは、BOT1 の読みを残します。

元のファイルは書き換えません。食い違いは merge_report.txt に書き出します。
"""
import json
import sys
from collections import Counter
from pathlib import Path


def load(path: Path) -> dict:
    if not path.exists():
        print(f"  （{path} はありません。空として扱います）")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} の形式が正しくありません")
    return data


def migrated_defaults(folder: Path, current: dict) -> set:
    """
    旧版が自動で入れていた「VOICEROID2の0番」を、移行でそのまま固定しただけの人。
    本人が選んだ声ではないので、順位を下げる。移行前のバックアップと見比べて判断する
    """
    bak = folder / "user_settings.json.bak-before-migration"
    if not bak.exists():
        return set()
    with open(bak, "r", encoding="utf-8") as f:
        old = json.load(f)
    cands = {}
    for uid, o in old.items():
        c = current.get(uid)
        if not isinstance(o, dict) or not isinstance(c, dict):
            continue
        was_default = o.get("voice") is None and o.get("voice_index") == 0 and (o.get("engine") or "roid") == "roid"
        same_tune = c.get("speed", 1.0) == o.get("speed", 1.0) and c.get("pitch", 1.0) == o.get("pitch", 1.0)
        if was_default and same_tune and c.get("engine") == "roid" and not c.get("auto") and c.get("voice"):
            cands[uid] = c["voice"]
    if not cands:
        return set()
    # 移行は全員に同じ「0番の声」を書いたので、いちばん多い声が0番。あとで別の声を選び直した人は除く
    zero = Counter(cands.values()).most_common(1)[0][0]
    return {uid for uid, v in cands.items() if v == zero}


def rank(e, uid=None, defaults=frozenset()) -> int:
    if not isinstance(e, dict):
        return -1
    if uid in defaults:
        return 1   # 移行で固定しただけの、旧版の初期値
    if e.get("voice") is not None and e.get("engine"):
        return 2 if e.get("auto") else 3
    if "voice_index" in e:
        return 1
    return 0


def describe(e) -> str:
    if not isinstance(e, dict):
        return "（なし）"
    if e.get("voice") is not None:
        kind = "自動" if e.get("auto") else "選択"
        v = f"{e.get('engine')}:{e.get('voice')}（{kind}）"
    elif "voice_index" in e:
        v = f"{e.get('engine') or 'roid'}:番号{e.get('voice_index')}（旧形式）"
    else:
        v = "未設定"
    return f"{v} 速度{e.get('speed', 1.0)} ピッチ{e.get('pitch', 1.0)}"


def same_voice(a: dict, b: dict) -> bool:
    keys = ("engine", "voice", "voice_index", "speed", "pitch")
    return all(a.get(k) == b.get(k) for k in keys)


def merge_users(u1: dict, u2: dict, report: list, def1=frozenset(), def2=frozenset()) -> dict:
    out = {}
    conflicts = only1 = only2 = took2 = 0
    for uid in sorted(set(u1) | set(u2)):
        a, b = u1.get(uid), u2.get(uid)
        if b is None:
            out[uid] = a
            only1 += 1
            continue
        if a is None:
            out[uid] = b
            only2 += 1
            continue
        ra, rb = rank(a, uid, def1), rank(b, uid, def2)
        chosen, other, src = (b, a, "BOT2") if rb > ra else (a, b, "BOT1")
        why = "旧版の初期値より、選んだ声を優先" if max(ra, rb) >= 2 and min(ra, rb) == 1 else ""
        out[uid] = chosen
        if src == "BOT2":
            took2 += 1
        if isinstance(other, dict) and isinstance(chosen, dict) and rank(other) >= 1 and not same_voice(chosen, other):
            conflicts += 1
            both_chosen = ra == 3 and rb == 3
            note = "【両方のBOTで自分で選んだ声が違う】" if both_chosen else (f"（{why}）" if why else "")
            report.append(f"user {uid}: {src}を採用 = {describe(chosen)} ／ 捨てた方 = {describe(other)} {note}".rstrip())
    print(f"  ユーザー: 合計{len(out)}人（BOT1のみ{only1}人、BOT2のみ{only2}人、"
          f"両方にいてBOT2を採用{took2}人、食い違い{conflicts}人）")
    return out


def merge_dict(d1: dict, d2: dict, report: list) -> dict:
    out = {}
    conflicts = added = 0
    for gid in sorted(set(d1) | set(d2)):
        m1 = d1.get(gid) or {}
        m2 = d2.get(gid) or {}
        merged = dict(m2)
        merged.update(m1)   # 同じ語は BOT1 を優先
        for w, r in m2.items():
            if w in m1 and m1[w] != r:
                conflicts += 1
                report.append(f"辞書 guild {gid}: 「{w}」 BOT1の読み「{m1[w]}」を採用 ／ BOT2の読み「{r}」")
            elif w not in m1:
                added += 1
        out[gid] = merged
    total = sum(len(m) for m in out.values())
    print(f"  辞書: 合計{total}語（BOT2から追加{added}語、読みの食い違い{conflicts}語）")
    return out


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(1)
    dir1, dir2, out_dir = (Path(a) for a in sys.argv[1:4])
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = [out_dir / "user_settings.json", out_dir / "dictionary.json"]
    exists = [p for p in targets if p.exists()]
    if exists:
        raise SystemExit(f"出力先にすでにファイルがあります（上書きしません）: {', '.join(map(str, exists))}")

    report: list = []
    print("ユーザー設定をまとめています…")
    u1, u2 = load(dir1 / "user_settings.json"), load(dir2 / "user_settings.json")
    d1, d2 = migrated_defaults(dir1, u1), migrated_defaults(dir2, u2)
    print(f"  旧版の初期値（VOICEROID2の0番）を移行で固定しただけの人: BOT1 {len(d1)}人、BOT2 {len(d2)}人")
    users = merge_users(u1, u2, report, d1, d2)
    print("辞書をまとめています…")
    dic = merge_dict(load(dir1 / "dictionary.json"), load(dir2 / "dictionary.json"), report)

    for path, data in zip(targets, (users, dic)):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    rep = out_dir / "merge_report.txt"
    with open(rep, "w", encoding="utf-8") as f:
        f.write("\n".join(report) if report else "食い違いはありませんでした")
    print(f"\n完了: {out_dir} に user_settings.json と dictionary.json を作りました")
    print(f"食い違いの詳細: {rep}")
    print(f"両方のBOTの config.txt で  shared_dir = {out_dir}  を指定してから起動してください")


if __name__ == "__main__":
    main()
