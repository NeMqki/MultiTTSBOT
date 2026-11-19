# MultiTTSBOT

Discord の読み上げBOT。VOICEROID2 / A.I.VOICE / VOICEVOX / CeVIO を1つのBOTでまとめて使えます。

## 主な機能
- 設定は `/settings` の画面にまとめています（自分の声・辞書・サーバー設定・BOTの表示と再起動）
- 4つのエンジンの話者を、エンジン → キャラクター → 話し方のメニューで選択。名前の一部でも検索できます
- 話者カタログ：自分の声を変えずに、ほかの声を試し聞きしてから選べます（試し聞きは本人にだけ届き、VCには流れません）
- 話者を設定していない人には、ランダムに声を割り当て
- 再起動後の自動復帰、エンジンの自動再接続、障害の通知
- 読み上げ辞書（一覧を見ながら追加・編集・削除・検索）、音量・速度・ピッチの調整
- 再生中に次の発言を先に合成（先読み）
- 入ってよいサーバーの制限（`allowed_guilds`）、BOT同士で設定を共有（`shared_dir`）

**使い方と、自分で動かす手順のページ:** https://astralbot.nemqki.moe/tts/

## 必要なもの
- Windows（VOICEROID2 / A.I.VOICE / CeVIO を使う場合）
- Python 3.10 以上、discord.py 2.6 以上（`requirements.txt` で入ります）
- [FFmpeg](https://ffmpeg.org/)（PATH を通す）
- 使いたい音声エンジン（各自で用意・インストール）

## セットアップ
```
pip install -r requirements.txt
```
VOICEROID2 を使うときは、[pyvcroid2](https://github.com/Nkyoku/pyvcroid2) も別に入れてください（PyPI にはありません）。

1. [Discord Developer Portal](https://discord.com/developers/applications) でBOTを作り、**Message Content Intent** を有効にする
2. `token.txt` を作り、BOTトークンを1行だけ書く（**絶対に公開しないでください。**）
3. `config.example.txt` を `config.txt` にコピーし、`owner_ids`（管理者のユーザーID）や `engines`（使うエンジン）を編集
4. 起動: `Start.bat` をダブルクリック（終了しても自動で起動し直します）。1回だけ動かすなら `python MultiTTSBOT.py`

BOTを複数動かすときは `python MultiTTSBOT.py config_bot2.txt` のように設定ファイルを指定し、`token_file` と `data_dir` をBOTごとに分けてください。設定と辞書を共有したいときは、両方の `shared_dir` に同じフォルダを指定します。

## 注意
- **CeVIO は同時に1つのアプリしか接続できません。** 複数のBOTで使うときは、片方だけにしてください。
- 各音声エンジン・音声ライブラリの**利用規約とクレジット表記は、利用者の責任**で守ってください（VOICEVOX などはクレジット表記が必須です。`/credit` で、使っている話者の一覧を表示します。`config.txt` の `credit_url` に書いたページへのリンクも出せます）。
- 音声エンジン本体・音声ライブラリは、このリポジトリに含まれません。
- 設定ファイル（`user_settings.json` など）にはユーザーIDが入ります。公開しないでください。
- ログ（`logs` フォルダ）には、サーバー名・チャンネル名・発言者の名前とIDが残ります。残したくないときは `log_reads = off` にしてください。

## 変更履歴
[patch_notes.md](patch_notes.md) を参照。
