---
name: rk_codesearch
description: 检索代码项目中的类、方法、路径和引用位置。适用于先定位代码，再回到本地工作区读文件和修改。优先用于：查类定义、查方法定义、查方法引用、根据全限定名定位源码文件。不要把结果当成本地绝对路径，结果中的 project 和 path 需要分开使用。
allowed-tools: Bash, Read, Grep, Glob
---

# rk_codesearch

`rk_codesearch` 只负责定位代码，不负责读取本地工作区文件，不负责 Git 历史快照。

它的结果应当作为后续本地读文件和改代码的入口信息：

- `project`
- `path`
- `line`

## 什么时候用

- 用户要找类定义、方法定义、引用位置
- 用户只知道全限定名，需要先定位源码文件
- 需要跨项目搜索某个符号或路径
- 需要先确认代码在哪，再去本地工作区改

## 什么时候不要用

- 不要把它当作本地文件读取器
- 不要把它当作 Git 历史快照接口
- 不要把输出路径直接当成本地绝对路径

## 关键规则

### 1. 一律使用 `keywords`

不要使用 `query`，当前接口没有这个参数。

单个关键词：

```bash
python3 /Users/bianjinchen/remote-run-plugins/commands/rk_codesearch/run.py search --keywords getPidsByName
```

多个关键词：

```bash
python3 /Users/bianjinchen/remote-run-plugins/commands/rk_codesearch/run.py search --keywords ProcessUtils,getPidsByName
```

每个 token 不能包含空格。多个 token 用逗号分隔。

### 2. 优先使用精确查询

查类定义，优先传全限定名：

```bash
--keywords com.rockchip.generic.ProcessUtils --search-field def --type java
```

查方法定义，优先传全限定方法名：

```bash
--keywords com.rockchip.generic.ProcessUtils.getPidsByName --search-field def --type java
```

查方法引用，优先传全限定方法名：

```bash
--keywords com.rockchip.generic.ProcessUtils.getPidsByName --search-field symbol --type java
```

如果只知道方法名，也可以直接搜：

```bash
--keywords getPidsByName --type java
```

### 3. 多关键词时显式决定语义

交集：

```bash
--keywords ProcessUtils,getPidsByName --keyword-mode and
```

并集：

```bash
--keywords ProcessUtils,getPidsByName --keyword-mode or
```

多关键词不适合 `smart` 的符号拆解时，插件会自动按全文查询处理。

### 4. 尽量限制项目和类型

如果用户知道项目，传 `--project`。

```bash
--project Android16
```

如果用户知道语言，传 `--type`。支持值只有：

- `c`
- `cxx`
- `java`
- `kotlin`
- `python`
- `sh`
- `golang`
- `rust`

### 5. 正确理解结果

结果示例：

```text
[definition] vendor/rockchip/platform/frameworks/magicboost/service/src/com/rockchip/generic/ProcessUtils.java
  project: Android16
  15:     public static ArrayList<Integer> getPidsByName(String processName) {
```

解释：

- 第一行路径是项目内相对路径
- `project:` 才是项目名
- 不要把 `project/path` 直接当成本地绝对路径

后续如果需要本地读文件，应当：

1. 先拿到 `project`
2. 再拿到 `path`
3. 通过本地工作区映射找到真实文件

## 推荐查询模式

### 查类定义

```bash
python3 /Users/bianjinchen/remote-run-plugins/commands/rk_codesearch/run.py search \
  --keywords com.rockchip.generic.ProcessUtils \
  --search-field def \
  --type java
```

### 查方法定义

```bash
python3 /Users/bianjinchen/remote-run-plugins/commands/rk_codesearch/run.py search \
  --keywords com.rockchip.generic.ProcessUtils.getPidsByName \
  --search-field def \
  --type java
```

### 查方法引用

```bash
python3 /Users/bianjinchen/remote-run-plugins/commands/rk_codesearch/run.py search \
  --keywords com.rockchip.generic.ProcessUtils.getPidsByName \
  --search-field symbol \
  --type java
```

### 类名 + 方法名联合查找

```bash
python3 /Users/bianjinchen/remote-run-plugins/commands/rk_codesearch/run.py search \
  --keywords ProcessUtils,getPidsByName \
  --keyword-mode and \
  --type java
```

## 工作方式

建议流程：

1. 先用 `rk_codesearch` 定位
2. 提取 `project`、`path`、`line`
3. 再回本地工作区读取真实文件
4. 本地修改并验证
5. 用 Git 生成补丁

不要跳过第 2 步，尤其不要把结果中的相对路径直接当成本地绝对路径。
