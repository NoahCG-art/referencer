# ローカルヘルパー — URLから動画を取り込む

## なぜ必要か

ブラウザは YouTube の映像ピクセルに触れられません。埋め込めるのはクロスオリジンの
iframe で、その中身は `canvas` に描けず `getImageData` も通らない。ストリームURLも
署名付きで `fetch` できない。**ブラウザのセキュリティ設計上そうなっている**ので、
回避策はありません。

そこでこのヘルパーが、

1. URL を受け取って `yt-dlp` で動画を手元に落とし
2. `http://127.0.0.1:8787` から配信する（Range対応）

という2つだけを担当します。カット検出はこれまで通りブラウザ側で行います。
動画も解析結果も、このPCから出ません。

## 導入（初回だけ）

### 1. Python を入れる

コマンドプロンプト（Mac ならターミナル）で確認:

```bash
python --version
```

`3.9` 以上が表示されればOK。`python が見つかりません` と出たら:

- **Windows**: https://www.python.org/downloads/windows/ からインストーラを落とし、
  最初の画面の **「Add python.exe to PATH」に必ずチェック**を入れてから Install
- **Mac**: 標準で `python3` が入っています。上のコマンドを `python3 --version` で試してください

### 2. yt-dlp を入れる

```bash
pip install yt-dlp
```

Mac で通らない場合は `pip3 install yt-dlp`。

Python を入れたくない場合は、`yt-dlp.exe`（単体ファイル）を
https://github.com/yt-dlp/yt-dlp/releases から落として
**このフォルダに置く**だけでも動きます。ただしヘルパー本体には Python が要ります。

### 3. ffmpeg を入れる（推奨）

映像と音声が別々に配信されている動画を結合するのに使います。無くても
だいたい動きますが、画質の選択肢が狭まります。

- **Windows**: `winget install ffmpeg`
- **Mac**: `brew install ffmpeg`

## 使う

```bash
python cutboard-helper.py
```

こう出れば起動しています:

```
カットボード ローカルヘルパー
  保存先 : .../helper/media
  yt-dlp : yt-dlp (2026.11.23)
  待受   : http://127.0.0.1:8787
  終了   : Ctrl+C
```

この黒い窓は**開いたまま**にしておきます。あとは `app/cutboard.html` を
ブラウザで開き、上の欄に動画URLを貼って Enter。

- ヘルパーが見つかると、右上に緑で「ヘルパー接続済み」と出ます
- 落とした動画は `helper/media/` に貯まります
- ページを開き直しても、動画は `localhost` の固定URLを持つので**選び直し不要**です

## 注意

- **ヘルパーを止めると、取り込んだ動画は再生できません**（カットのサムネと時刻は残ります）。
  また起動すれば戻ります
- YouTube からの動画ダウンロードは YouTube の利用規約に反します。手元での参照・
  研究目的であっても形式上はそうなので、把握した上で判断してください
- 取得形式は H.264 の mp4 を優先します。精密解析（WebCodecs + MP4Box）が
  効くのが mp4 コンテナのためです

## 困ったとき

| 症状 | 原因と対処 |
|---|---|
| 「ヘルパー未起動」が消えない | 黒い窓が閉じている。`python cutboard-helper.py` を実行 |
| 「yt-dlp が未導入です」 | `pip install yt-dlp`、または `yt-dlp.exe` をこのフォルダに置く |
| 取り込みが「失敗」になる | 窓に出るエラーを確認。多くは URL 違い、年齢制限、地域制限 |
| 進まない／遅い | 大きい動画は時間がかかります。進捗バーが動いていれば正常 |
| ポートが使用中と出る | 他のアプリが 8787 を使っている。`PORT` を書き換えてください |

## API（開発用）

| | |
|---|---|
| `GET /health` | 稼働確認、yt-dlp の有無 |
| `POST /fetch` | `{"url": "..."}` でダウンロード開始 |
| `GET /jobs` | 進捗 |
| `GET /library` | 手元の動画一覧 |
| `GET /media/<file>` | 動画本体（Range対応・CORS許可） |
| `DELETE /library/<file>` | 削除 |
