---
name: video-vocab
description: >-
  Extracts unknown English words and phrases from a video (MP4 or other file,
  with or without subtitles) for a learner at about 6000 words, writes
  Chinese-English flashcards, and records what was studied per video and
  across all videos. Use when the user mentions a video, MP4, subtitles,
  vocab cards, flashcards, 视频, 字幕, 单词卡, 背单词, or the video vocabulary ledger.
---

# 视频生词卡

从一支视频里抽出相对约 6000 词量的生词和短语，写成中英单词卡，并记入账本。脚本负责抽文本、筛选和记账。中文释义由你根据原句来写。

视频只在本机处理。不要上传视频，也不要把模型编出来、原文里没有的表达写成卡片。

## 环境

命令都在项目根目录运行。Python 用 `.venv/Scripts/python`。

若虚拟环境或 `data/known_lemmas.txt` 还不存在：

1. `winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements`
2. `winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements`
3. `python -m venv .venv`
4. `.venv/Scripts/python -m pip install -r requirements.txt`
   如果连不上 PyPI，改用：`.venv/Scripts/python -m pip install -r requirements.txt -i http://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com`
5. `.venv/Scripts/python .cursor/skills/video-vocab/scripts/build_known_list.py`

没有字幕、需要听写时再安装：`.venv/Scripts/python -m pip install -r requirements-asr.txt`。听写模型会下到 `models/whisper/`。普通 CPU 上，一小时视频可能要十几到几十分钟。

## 处理一支视频

把下面清单复制出来并逐项做完：

```
- [ ] 抽出文本
- [ ] 选出候选
- [ ] 写中文释义或标忽略
- [ ] 导入账本
- [ ] 再选一次，补上新出现的候选
- [ ] 生成卡片
- [ ] 把卡片路径、条数和听写警告告诉用户
```

1. 抽文本。有同名字幕或内嵌字幕时不会听写。

```
.venv/Scripts/python .cursor/skills/video-vocab/scripts/extract_text.py "C:/path/video.mp4"
```

旁边有别的字幕文件时加 `--subtitle "C:/path/video.srt"`。

2. 选候选。默认每支视频最多 25 个生词、10 条短语。

```
.venv/Scripts/python .cursor/skills/video-vocab/scripts/select_candidates.py --transcript data/transcripts/SLUG.json
```

3. 读候选 JSON。为每个新词和新短语写释义文件，例如 `data/candidates/SLUG.gloss.json`：

```json
[
  {"key": "persist", "zh": "坚持，不放弃"},
  {"key": "look forward to", "zh": "期待"},
  {"key": "harry", "ignore": true}
]
```

释义规则：

- `key` 必须和候选里的完全一致，不能新造。
- 中文只写这一句里的意思，短语按整条表达来译，不要逐词硬译，也不要写百科。
- 人名、品牌、听写垃圾用 `ignore: true`，不要写释义。

4. 导入。

```
.venv/Scripts/python .cursor/skills/video-vocab/scripts/ledger.py import-video --transcript data/transcripts/SLUG.json --candidates data/candidates/SLUG.json --glosses data/candidates/SLUG.gloss.json
```

5. 再运行一次第 2 步。忽略项腾出名额后，可能会多出新词。只给还没有释义的 key 补文件，然后再导入一次。不要循环超过两次。

6. 短语还不满 10 条、而且原文里确有一条值得学的表达时，先验证再加入。验证结果里的原句必须就是这个意思。

```
.venv/Scripts/python .cursor/skills/video-vocab/scripts/select_candidates.py --transcript data/transcripts/SLUG.json --verify "come up with"
.venv/Scripts/python .cursor/skills/video-vocab/scripts/ledger.py add-phrase --transcript data/transcripts/SLUG.json --phrase "come up with" --zh "想出"
```

7. 生成卡片。

```
.venv/Scripts/python .cursor/skills/video-vocab/scripts/render_cards.py
```

把总览页 `cards/index.html` 和这支视频的卡片页告诉用户。若 `source` 是 `asr`，或文本带 `warning`，要明确说这些句子是机器听写，不对就点忽略。

## 账本里的状态

- `candidate` 待决定：挑出来了，还没算「在背」。
- `learning` 在背：用户点了「加入背诵」。这支视频和全部视频的总账都算它。
- `known` 已认识：写入 `data/known_lemmas.txt` 或 `data/known_phrases.txt`，以后不再推荐。
- `ignored` 忽略：全局不再推荐。

「在背」不是「已经记住」。不要在导入时自动标成在背。

用户导出 `marks.json` 或说「导入标记」后：

```
.venv/Scripts/python .cursor/skills/video-vocab/scripts/ledger.py import-marks path/to/marks.json
.venv/Scripts/python .cursor/skills/video-vocab/scripts/render_cards.py
```

查看总账或某一支：

```
.venv/Scripts/python .cursor/skills/video-vocab/scripts/ledger.py list
.venv/Scripts/python .cursor/skills/video-vocab/scripts/ledger.py list --video SLUG
```

## 词表

`data/known_lemmas.txt` 是大约 6000 个高频词条，可以手改。词形会合并，`running` 不会在已经认识 `run` 时再出现。重新生成会补回被删掉的高频词，但会留下文件里多出来的词：

```
.venv/Scripts/python .cursor/skills/video-vocab/scripts/build_known_list.py --force
```

短语表在 `data/phrases.txt`。一行是「优先级 + 制表符 + 短语」。优先级数字越大，超过 10 条时越优先保留。
