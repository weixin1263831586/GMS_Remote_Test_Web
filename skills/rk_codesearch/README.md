# rk_codesearch

本插件用于代码检索，只做两件事：

- 列出可搜索项目
- 搜索代码并返回 `project + path + line`

它适合作为“定位器”使用：

1. 先用 `search` 找到项目、文件和行号
2. 再去本地工作区读取真实文件
3. 本地修改并用 Git 生成补丁

不要把搜索结果直接当作本地绝对路径。输出中的 `path` 是项目内相对路径，`project` 会单独给出。

## 动作

### `list_projects`

列出服务端可搜索项目。

示例：

```bash
python3 commands/rk_codesearch/run.py list_projects
```

### `search`

执行代码搜索。

示例：

```bash
python3 commands/rk_codesearch/run.py search --keywords getPidsByName
python3 commands/rk_codesearch/run.py search --keywords ProcessUtils,getPidsByName --type java
python3 commands/rk_codesearch/run.py search --keywords com.rockchip.generic.ProcessUtils --search-field def --type java
python3 commands/rk_codesearch/run.py search --keywords ProcessUtils,getPidsByName --keyword-mode or --project Android16 --type java
```

## 参数

### `keywords`

必填。逗号分隔的搜索 token。

规则：

- 单个关键词也走这个参数，比如 `getPidsByName`
- 多个关键词用逗号分隔，比如 `ProcessUtils,getPidsByName`
- 每个 token 不能包含空格

插件会在内部把多个 token 组装成服务端可接受的查询串，避免直接传带空格的查询参数。

### `keyword_mode`

可选。多关键词连接方式：

- `and`：默认
- `or`

组装规则：

- `keywords=ProcessUtils,getPidsByName` + `keyword_mode=and`
  实际查询串为 `ProcessUtils AND getPidsByName`
- `keywords=ProcessUtils,getPidsByName` + `keyword_mode=or`
  实际查询串为 `ProcessUtils OR getPidsByName`

### `search_field`

可选。搜索字段：

- `smart`：默认。自动识别类名、方法名、全限定名、路径，并优先返回定义
- `full`
- `path`
- `def`
- `symbol`

兼容别名：

- `auto` -> `smart`
- `defs` -> `def`
- `ref` -> `symbol`
- `refs` -> `symbol`

说明：

- 多关键词查询在 `smart` 模式下会自动按 `full` 处理
- `def` 更偏向返回定义
- `symbol` 更偏向返回引用

### `project`

可选。项目过滤。多个项目用逗号分隔。

示例：

```bash
--project Android16
--project Android14,Android16
```

未传时会先使用配置里的 `default_projects`。如果默认项目无命中，插件会在合适的查询类型下自动回退到全项目搜索。

### `type`

可选。代码类型过滤。只接受以下短值：

- `c`
- `cxx`
- `java`
- `kotlin`
- `python`
- `sh`
- `golang`
- `rust`

内部映射：

- `c` -> `C`
- `cxx` -> `C++`
- `java` -> `Java`
- `kotlin` -> `Kotlin`
- `python` -> `Python`
- `sh` -> `Shell script`
- `golang` -> `Golang`
- `rust` -> `Rust`

### `limit`

可选。最多返回多少个文件结果。

未传时使用配置中的 `default_limit`，默认是 `15`。

## 输出格式

输出包含：

- `time_ms`
- `result_count`
- `returned_files`
- `project_scope`

每条结果按以下格式打印：

```text
[definition] vendor/rockchip/platform/frameworks/magicboost/service/src/com/rockchip/generic/ProcessUtils.java
  project: Android16
  15:     public static ArrayList<Integer> getPidsByName(String processName) {
```

其中：

- 方括号里的值表示结果类型：`definition`、`reference`、`path`、`text`
- 第一行路径是项目内相对路径，不带项目名前缀
- `project:` 单独输出，避免把项目名误当路径

## 配置

配置文件：

- [config.json](/Users/bianjinchen/remote-run-plugins/commands/rk_codesearch/config/config.json)

当前支持：

- `base_url`
- `token`
- `default_projects`
- `default_limit`

环境变量：

- `RK_CODESEARCH_URL`
- `RK_CODESEARCH_TOKEN`

## 建议用法

- 查类定义：`--keywords com.xxx.ClassName --search-field def`
- 查方法定义：`--keywords com.xxx.ClassName.methodName --search-field def`
- 查方法引用：`--keywords com.xxx.ClassName.methodName --search-field symbol`
- 多关键词交集：`--keywords ClassName,methodName --keyword-mode and`
- 多关键词并集：`--keywords keyword1,keyword2 --keyword-mode or`

推荐流程：

1. 用 `rk_codesearch` 找位置
2. 用 `project + path` 映射到本地工作区
3. 读取本地文件并修改
4. 用 Git 生成补丁
