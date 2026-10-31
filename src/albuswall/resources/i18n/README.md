# resources/i18n

运行期翻译文件（`QTranslator` 直接加载这里的 `.qm`）。

```
albuswall_en_US.ts   albuswall_en_US.qm
albuswall_zh_CN.ts   albuswall_zh_CN.qm
```

* `.ts` 是**可编辑源**：可以用 `pyside6-linguist` 打开、修改译文；
* `.qm` 是**生成物**：由 `pyside6-lrelease` 从 `.ts` 编译，运行期只读 `.qm`。

## 约定

界面源文目前统一为**中文**：默认语言是 `zh_CN`，所以即使一个翻译器都没装，
界面也是可读的。据此：

* `zh_CN` —— 源文本身就是目标语言，不写条目（`<translation type="unfinished" />`），
  Qt 自动回退到源文；
* `en_US` —— 需要补齐全部条目，缺译文即视为未完成。

因此新增界面文案时**只写中文源文**，再按下面的流程把 `en_US` 补上即可。

## 运行期切换

三个等价入口，参数都直接给语言代码字面量：

```python
from albuswall.ui import set_language          # 模块级，最省事
set_language("en_US")                          # 也接受 "en" / "en-US" / "English"
set_language("zh_CN")                          # 也接受 "zh" / "zh-CN" / "简体中文"

app.set_language("en_US")                      # albuswall.ui.Application 实例
container.get("i18n").set_language("en_US")    # 依赖容器
```

未知写法返回 `False`（当前语言不变）；启动语言取自配置 `[ui] language`，
没有配置时用 `albuswall.ui.languages.DEFAULT_LANGUAGE`。

切换后控件在事件循环下一轮刷新（Qt 广播的是 post 事件），所以调用后不要
立刻断言界面文字 —— `app.processEvents()` 之后再看。

## 重新生成

`pyside6-lupdate` 只提取**字面量** `tr("...")`：文案不要经过变量（例如
`self.tr(SOME_DICT[key])` 不会被提取），需要时把字面量写在方法里。

```bash
cd <repo 根目录>

# 1) 从源码提取（-no-obsolete 顺手丢掉已删模块的旧条目）
pyside6-lupdate $(grep -rl 'self\.tr(' --include='*.py' src) \
    -ts src/albuswall/resources/i18n/albuswall_en_US.ts -no-obsolete
pyside6-lupdate $(grep -rl 'self\.tr(' --include='*.py' src) \
    -ts src/albuswall/resources/i18n/albuswall_zh_CN.ts -no-obsolete

# 2) 补译文：用 pyside6-linguist 填 en_US 里 type="unfinished" 的条目

# 3) 编译运行期 .qm（en_US 应显示 0 unfinished）
pyside6-lrelease src/albuswall/resources/i18n/albuswall_en_US.ts \
    -qm src/albuswall/resources/i18n/albuswall_en_US.qm
pyside6-lrelease src/albuswall/resources/i18n/albuswall_zh_CN.ts \
    -qm src/albuswall/resources/i18n/albuswall_zh_CN.qm
```

翻译上下文（`<context>`）就是界面类的类名，改类名等于换了一套 key，
需要同步更新 `.ts`。
