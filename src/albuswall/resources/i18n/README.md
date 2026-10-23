# resources/i18n

运行期翻译文件（`QTranslator` 直接加载这里的 `.qm`）。

```
albuswall_en_US.ts   albuswall_en_US.qm
albuswall_zh_CN.ts   albuswall_zh_CN.qm
```

* `.ts` 是**可编辑源**：可以用 `pyside6-linguist` 打开、修改译文；
* `.qm` 是**生成物**：`scripts/build_i18n.py` 里调 `pyside6-lrelease` 产出，
  运行期只读 `.qm`。

界面源文是中英混排的，所以两个语种各补自己缺的部分（英文源文只进
`zh_CN`，中文源文只进 `en_US`）；源文本身就是目标语言时不写条目，Qt 会
自动回退。

## 重新生成

```bash
python scripts/build_i18n.py            # 提取新文案 + 补缺失译文 + 编译 .qm
python scripts/build_i18n.py --refresh  # 用 scripts/i18n_catalog.py 覆盖旧译文
python scripts/build_i18n.py --check    # CI：缺译文时退出码非 0
```

翻译词表在 `scripts/i18n_catalog.py`；详细约定见 `docs/i18n.md`。
