# MultiTTSBOT

Discord の読み上げBOT。VOICEROID2 / A.I.VOICE / VOICEVOX / CeVIO を1つのBOTでまとめて使えます。

## 主な機能
- 4つのエンジンの話者を、`/voice` でまとめて検索・選択（名前の一部で検索できます）
- 話者を設定していない人には、ランダムに声を割り当て
- 再起動後の自動復帰、エンジンの自動再接続、障害の通知
- 読み上げ辞書（`/dict_add`）、音量・速度・ピッチの調整
- 再生中に次の発言を先に合成（先読み）

## 必要なもの
- Windows（VOICEROID2 / A.I.VOICE / CeVIO を使う場合）
- Python 3.10 以上
- [FFmpeg](https://ffmpeg.org/)（PATH を通す）
- 使いたい音声エンジン（各自で用意・インストール）

## セットアップ
```
pip install -r requirements.txt
```
1. [Discord Developer Portal](https://discord.com/developers/applications) でBOTを作り、**Message Content Intent** を有効にする
2. `token.txt` を作り、BOTトークンを1行だけ書く（**絶対に公開しないでください。**）
3. `config.example.txt` を `config.txt` にコピーし、`owner_ids`（管理者のユーザーID）や `engines`（使うエンジン）を編集
4. 起動: `python MultiTTSBOT.py`

BOTを複数動かすときは `python MultiTTSBOT.py config_bot2.txt` のように設定ファイルを指定し、`token_file` と `data_dir` をBOTごとに分けてください。

## 注意
- **CeVIO は同時に1つのアプリしか接続できません。** 複数のBOTで使うときは、片方だけにしてください。
- 各音声エンジン・音声ライブラリの**利用規約とクレジット表記は、利用者の責任**で守ってください（VOICEVOX などはクレジット表記が必須です。`/credit` で案内を表示します）。
- 音声エンジン本体・音声ライブラリは、このリポジトリに含まれません。
- 設定ファイル（`user_settings.json` など）にはユーザーIDが入ります。公開しないでください。

## 変更履歴
[patch_notes.md](patch_notes.md) を参照。
