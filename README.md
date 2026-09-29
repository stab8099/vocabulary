# 视频生词卡

给大约 6000 词水平的英语学习者用。程序读一支视频，挑出其中可能还不认识的单词和短语，做成中英单词卡，并记下每支视频背了什么、至今所有视频一共背了什么。

视频只在本机处理，不上传。

## 目标

1. 读取 MP4 等视频。有外挂字幕或内嵌字幕时用字幕；完全没有字幕时，在本机把声音转成文字。
2. 按大约 6000 词的已知词表，挑出刚好超纲的生词，以及原文里真实出现的短语。不收录人名、品牌和只出现一次的极罕见词。每支视频默认最多 25 个生词、10 条短语。
3. 做成中英单词卡：正面英文，背面中文、原句和时间点。中文必须对得上这一句的意思。
4. 用账本分开记录「这支视频在背的」和「全部视频在背的」。同一条表达再次出现时，总账只有一条，下面挂多次出现。
5. 学习者可以标认识、加入背诵或忽略。认识的以后不再推荐，忽略的全局不再推荐。标成在背才算进总账，不表示已经记住。

做不到、也不承诺的事：一次就精确知道你个人不会哪些词；把模型编出来、原文里没有的句子写成卡片。

## 在另一台电脑上试

不要把本机的 `.venv` 拷过去，虚拟环境不能跨电脑用。视频文件很大，也不用放进仓库。`.gitignore` 已经排除了虚拟环境、视频、听写模型和每支视频的中间文本。

需要 Python 3.12 和 FFmpeg。在项目根目录执行：

```text
winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

如果连不上 PyPI，把最后一条换成：

```text
.venv\Scripts\python -m pip install -r requirements.txt -i http://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

仓库里已经带有 `data/known_lemmas.txt`。若这台电脑上没有该文件，再运行：

```text
.venv\Scripts\python .cursor/skills/video-vocab/scripts/build_known_list.py
```

先确认程序本身能跑通：

```text
.venv\Scripts\python -m unittest tests.test_pipeline
```

再用一支真实视频试。在 Cursor 里打开本文件夹，说「处理这个视频」，并给出 mp4 路径。处理完打开 `cards/index.html`。

建议按这个顺序看结果：

- 有字幕的视频：卡片上的英文能在字幕原句里找到，中文对得上那一句。
- 没有字幕的视频：先安装 `.venv\Scripts\python -m pip install -r requirements-asr.txt`。卡片应标明句子来自机器听写。
- 点「加入背诵」并导出标记，让 Cursor 导入之后，总览里能看到这支视频，也能看到全部在背条目。
- 再处理第二支出现同一表达的视频，总账里这条仍然只有一条。

没有字幕时，一小时视频在普通 CPU 上可能要十几到几十分钟。详细步骤在 `.cursor/skills/video-vocab/SKILL.md`。
