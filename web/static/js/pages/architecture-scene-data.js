/* Six static topology compositions; see canonical Mermaid diagrams for exact protocol edges.
   These views never assert live device counts, module health, success rates or uptime. */
window.ArchitectureSceneData = Object.freeze({
 platform: {
  h:545, nodes:[
   ['entry','用户与入口','统一接入','users','access',34,92,226,166,['Web 工作台','主机桌面 / 终端','测试套件 / ATS',['对话 Agent','对话 / CLI Agent']]],
   ['controller','Controller','控制与调度中心','server','control',387,85,328,181,['认证与权限','任务调度','设备 Claim / Lease','设备管理','服务编排',['审计 / 报告索引','审计、报告索引与受控知识源周期调度']]],
   ['workers','Worker 集群','执行与结果采集','network','worker',861,92,215,166,[['Worker A','Linux Worker A'],['Worker B','Linux Worker B'],['Tradefed','Tradefed / 测试'],['原生结果 / 日志','原生 results / logs']]],
   ['external','外部协同与知识','受控集成','branch','external',34,350,210,143,['Redmine / Gerrit',['源码 / AI','SDK / CodeSearch / AI'],['RAG / Wiki / CDD','独立 Retrieval：混合检索、向量索引、证据覆盖；CDD / 已批准 Wiki 自动批次'],['构建服务','受审计 SSH / 构建服务器']]],
   ['usbip','USB/IP','设备通道','plug','channel',310,350,198,143,['USB 映射','设备透传','热插拔 / 重枚举']],
   ['device','测试设备','物理资源','device','resource',576,350,199,143,[['TV / STB','Android TV / STB'],'手机 / 平板','本地 USB','远端 USB']],
   ['adb','ADB Proxy','调试代理','terminal','teal',842,350,233,143,['ADB 连接','多设备代理','命令转发']]],
  edges:[
   ['M260 175 H387','command','HTTPS / Token',322,155],['M715 175 H861','command','Poll / 命令',788,155],
   ['M861 222 H715','report','Poll / ACK',788,246],
   ['M387 235 H285 V422 H244','command','受控协同',320,293],
   ['M409 266 V350','command','USB/IP',460,313],
   ['M958 258 V350','command','调试代理',1020,312],
   ['M897 258 V312 H676 V350','data','设备测试',796,290],
   ['M508 424 H576','both','USB 连接',542,403],['M775 424 H842','both','受控 ADB',809,403]
  ],
  legend:[['access','入口 / Agent'],['control','控制与调度'],['worker','Worker 执行'],['resource','设备 / USB'],['external','外部协同']],
  boundary:['Worker 主动 Heartbeat / Poll，命令随 Poll 返回并以 ACK 汇报。','USB/IP 转发 USB 总线；ADB Proxy 代理受控 ADB 会话。','Controller 通过受控接口集成工单、源码、模型、知识与构建服务。','Feature 通过公共接口 / Foundation Port 交互；Bootstrap 组装运行时依赖。']
 },
 usbip: {
  h:535,nodes:[
   ['android','Android 设备','物理 USB','device','resource',34,224,220,120,['设备直连','可重枚举 USB']],
   ['win','Windows Source','usbipd-win','monitor','channel',337,55,215,112,[['绑定 / 导出','设备绑定 / 导出'],'USB/IP 服务']],
   ['linux','Linux Source','usbipd','terminal','channel',337,402,215,112,[['绑定 / 导出','设备绑定 / 导出'],'USB/IP 服务']],
   ['control','Controller','协调与控制','server','control',337,224,215,120,['SSH / bind','权限 / 租约']],
   ['worker','Linux Worker','usbip attach','server','worker',698,224,185,120,['USB 接入','状态 / 回执']],
   ['vhci','虚拟 USB 总线','vhci_hcd','plug','resource',941,224,172,120,['USB 语义','设备映射']],
   ['tools','ADB / Fastboot','Tradefed','test','worker',941,407,172,113,['ADB 调试','Tradefed']]],
  edges:[
   ['M254 246 H286 V113 H337','data','选定 Source',283,190],['M254 321 H286 V459 H337','data','二选一接入',283,372],
   ['M552 110 H791 V224','data','USB/IP TCP 3240',669,94],
   ['M552 458 H791 V344','data','USB/IP TCP 3240',670,442],
   ['M552 263 H698','command','Poll / attach',625,246],['M698 314 H552','report','Poll / ACK',625,335],
   ['M883 284 H941','data','attach',912,216],['M1027 344 V407','data','本地 USB',1073,380],
   ['M444 224 V167','command','SSH / bind',498,199],['M444 344 V402','command','SSH / bind',498,382]
  ],
  legend:[['resource','Android 物理设备'],['channel','Source / USB/IP'],['control','Controller 协调'],['worker','Worker / Tradefed']],
  boundary:['Windows / Linux Source 各自运行 USB/IP 导出服务；SSH / bind 受审计。','物理 USB 设备经 TCP 3240 透传，Worker 使用 vhci_hcd。','租约与设备归属由 Controller 管理；重枚举须重新发现和 attach。'],
  cards:[
   ['接入链路分层','access',[['1','Source','导出物理 USB 设备'],['2','Worker','usbip attach 映射远端资源'],['3','Controller','协调绑定、租约、任务']]],
   ['关键职责 / 边界','worker',[['设备','物理 USB 连接'],['Source','导出 USB / 维护映射'],['Controller','SSH / bind；Poll 命令协调'],['Linux Worker','attach / ADB / 测试']]],
   ['常见故障点','risk',[['Source','服务未运行 / 未绑定'],['网络','TCP 3240 不可达'],['Worker','attach 或 vhci_hcd 异常'],['ADB','权限 / 驱动 / 重枚举状态']]]
  ]
 },
 aoa:{
  h:585,nodes:[
   ['web','AOA 工作台','Web','monitor','access',35,65,226,129,['录制 / 标定','Profile 选择']],
   ['control','Controller','AOA 服务','server','control',341,65,245,129,['权限 / 状态','回放调度']],
   ['sqlite','SQLite','Profile / 状态','archive','resource',843,65,218,129,['Profile 存储','任务状态']],
   ['worker','Worker','AOA 回放','settings','worker',342,257,225,125,['物理端口绑定','事件 / ACK']],
   ['aoa','AOAv2','Accessory / HID','plug','worker',640,257,197,125,[['启动 AOA','启动 Accessory'],'注入 HID']],
   ['android','Android 设备','开机向导','device','resource',908,257,180,125,['屏幕操作','USB 调试']],
   ['adb','ADB 状态','连通性检查','terminal','resource',908,454,180,103,[['ADB 检查','adb shell true'],['连通性','设备连通性']]]],
  edges:[
   ['M261 128 H341','data','HTTPS',300,108],['M586 128 H843','data','配置与状态',714,111],
   ['M437 194 V257','command','Poll / 回放',388,230],['M491 257 V194','report','事件 / ACK',550,230],
   ['M567 320 H640','data','AOAv2',602,299],['M837 320 H908','data','HID 注入',873,299],
   ['M998 382 V454','data','USB 调试确认',1040,425],
   ['M455 382 V506 H908','report','ADB liveness / 状态校验',685,483]
  ],
  legend:[['access','AOA 工作台'],['control','Controller / AOA'],['worker','回放 / AOAv2'],['resource','Profile / Android']],
  boundary:['Profile 按 owner 隔离，关联屏幕、密度、方向和版本。','AOAv2 / HID 注入是设备操作通道，不依赖已有 ADB。','AOA 回放完成不保证 ADB 可用，需检查调试授权与设备连通性。'],
  cards:[
   ['执行链路分层','access',[['AOA 工作台','录制、标定、选取 Profile'],['Controller','授权 / 调度 / 回报'],['Worker','执行并报告事件'],['AOAv2','Accessory / HID 输入'],['Android','向导完成 / USB 调试']]],
   ['Profile / 回放说明','worker',[['Profile','HID 参数与触点坐标'],['回放','Worker 按步骤执行'],['异常','关键步骤结果实时回报'],['适配','设备屏幕差异须重新校验']]],
   ['ADB 就绪判定','risk',[['1','完成开机向导配置'],['2','开启 USB 调试'],['3','执行 adb shell true'],['4','通过后进入后续测试']]]
  ]
 },
 firmware:{
  h:550,nodes:[
   ['firmware','固件库 / 共享固件','固件来源','package','access',28,26,218,139,['固件选择','机型 / 版本','路径 / 校验']],
   ['controller','Controller 调度','权限 / 归属 / 烧写编排','server','control',383,26,320,139,['任务创建','设备租约','来源路由','状态 / 回执']],
   ['worker','本地 USB Worker','本地 USB 执行','server','worker',859,26,218,139,['本地 USB','Fastboot / RK','执行 / 日志']],
   ['source','Windows Source Agent','交还后在 Source 执行','monitor','channel',28,225,218,135,['解除 USB/IP','RKDevTool','重建 USB/IP']],
   ['source-device','Source 物理设备','Android / USB','device','resource',365,225,218,135,['Loader / MaskROM','重枚举 / 重启']],
   ['loader','本地烧写工具','Loader / RockUSB / Fastboot','tool','worker',859,225,218,135,['upgrade_tool','fastboot','模式 / 镜像校验']],
   ['aoa','可选 AOA 恢复','向导 / 调试授权','settings','channel',365,420,218,105,['HID 回放','ADB 就绪']],
   ['device','本地 Android 设备','重启 / 实际结果','device','resource',859,420,218,105,['启动 / 版本','ADB 校验']]
  ],
  edges:[
   ['M246 95 H383','command','上传 / 校验',314,77],
   ['M703 84 H859','command','Poll / 烧写',781,66],
   ['M859 138 H703','report','状态 / ACK',781,155],
   ['M423 165 V193 H137 V225','command','交还 / 调度',280,184],
   ['M190 225 V208 H485 V165','report','结果 / 回执',550,190],
   ['M246 292 H365','data','RKDevTool',305,274],
   ['M968 165 V225','command','本地烧写',1021,195],
   ['M968 360 V420','data','重启 / 校验',1031,390],
   ['M900 165 V195 H750 V390 H474 V420','command','可选 AOA',610,378],
   ['M583 472 H859','data','ADB 校验',715,454]
  ],
  legend:[['access','固件输入'],['control','Controller / 调度'],['worker','本地 Worker / 工具'],['channel','Source / AOA'],['resource','物理设备']],
  boundary:['本地 Worker / Windows Source 是两条整包烧写路径；USB/IP 设备须先交还 Source。','Linux USB/IP 整包烧写不支持；ADB Proxy 不提供 Fastboot / RockUSB。','Loader / MaskROM 重枚举须重识别；烧写结束不等于启动与 ADB 校验成功。'],
  cards:[
   ['烧写阶段说明','access',[['1','固件选择与校验'],['2','权限、租约及来源确认'],['3','本地 Worker 或 Windows Source'],['4','模式识别 / 镜像写入'],['5','重启、校验与回执']]],
   ['执行与所有权','worker',[['本地 USB','Worker / upgrade_tool / Fastboot'],['Windows Source','交还 USB/IP / RKDevTool'],['Linux Source','整包烧写拒绝'],['AOA','按需恢复 / ADB 检查']]],
   ['风险与恢复','risk',[['模式切换','Loader / MaskROM 重枚举'],['设备失联','重新识别稳定设备身份'],['来源烧写','重建 USB/IP 连接'],['验证','真实启动、版本与 ADB 状态']]]
  ]
 },
 'test-flow':{
  h:680,nodes:[
   ['plan','测试计划 / 任务创建','测试入口','test','access',35,45,244,160,['测试计划管理','套件 / 用例选择','设备要求']],
   ['control','Controller 调度','任务与资源','server','control',400,48,282,151,['权限 / 预检','Claim / Lease',['任务队列','ATS / Cluster Job'],'状态审计']],
   ['worker','Worker 执行','Tradefed 测试运行','server','worker',843,45,235,160,['任务领取','Tradefed 执行','异常上报']],
   ['repair','修复验证 / Gerrit','回归验证','branch','channel',38,299,238,162,['修复变更','构建 / 再测试','结果比对']],
   ['prep','设备准备','执行前检查','shield','resource',393,260,294,120,['设备租约','镜像 / 环境','能力 / 健康检查']],
   ['engines','测试套件生态','Tradefed','package','control',393,444,294,118,['CTS','GTS','VTS','STS']],
   ['logs','Worker 原生结果','results / logs','chart','channel',839,340,237,165,[['Tradefed 日志','Logcat / Tradefed'],'Pass / Fail','原生 Workdir']],
   ['report','报告浏览 / 登记','按需汇总与分析','book','control',839,564,237,104,['结果查看',['按需登记 / 分析','单测诊断：公开 CDD 背景候选、来源版本及适用性限制']]],
   ['redmine','缺陷流转','工单 / 回归','workflow','access',42,564,230,104,['Redmine 工单','缺陷与版本关联']]],
  edges:[
   ['M279 125 H400','command','创建任务',338,108],['M682 125 H843','command','Poll / 任务',762,107],
   ['M983 205 V340','data','执行日志',1034,282],['M843 175 H682','report','事件 / ACK',762,193],
   ['M539 199 V260','command','预检 / 租约',603,230],['M540 380 V444','data','套件准备',594,420],
   ['M687 487 H790 V235 H930 V205','data','本地套件',748,465],['M958 505 V564','report','结果索引',1014,537],
   ['M839 616 H272','data','按需提单',552,593],['M157 564 V461','data','按需修复',209,514],
   ['M157 299 V205','report','回归反馈',210,270]
  ],
  legend:[['access','任务入口 / 缺陷'],['control','Controller / 套件'],['worker','Worker 执行'],['resource','设备与环境']],
  boundary:['任务与设备租约、原生工作目录均有明确属主。','Worker 失联不自动等于任务可跨主机续跑。','原生 results / logs 与 Controller 已登记报告分开；诊断按需进行。','修复应关联 Gerrit 变更和真实的回归验证结果。'],
  cards:[
   ['闭环阶段','access',[['计划','套件 / 设备 / 用例'],['调度','租约 / 能力预检'],['执行','Worker 跑 CTS / GTS / VTS / STS'],['分析','原生结果 / 失败项'],['修复','Redmine + Gerrit'],['回归','重跑并归档']]],
   ['角色边界','worker',[['测试工程师','任务配置 / 结果分析'],['Controller','资源、队列、状态'],['Worker','测试、日志、回执'],['开发 / 质量','修复 / 构建 / 验证']]],
   ['关键输出物','risk',[['报告','查看原生结果 / 已登记报告'],['Workdir','原生 Tradefed 结果'],['Redmine','工单证据与状态'],['Gerrit','代码变更与审查'],['知识','经验、规则与配置']]]
  ]
 },
 'agent-analysis':{
  h:675,nodes:[
   ['input','用户 / 对话 Agent','工单与工具规划','message','access',28,44,236,177,[['问题 / 工单','问题 / 工单 / 私有图片附件'],'每日晨报',['kkagent / CLI','kkagent / CLI / MCP'],'设备 / 版本']],
   ['control','Controller / 工具网关','权限门禁与任务状态','server','control',389,35,342,190,['Service Token','权限与审计','受控工具调用','任务校验 / 入队','run_id / 状态','报告索引']],
   ['worker','设备执行 Worker','按需授权 / 独立执行','server','worker',867,44,210,180,['Worker A','Worker B',['ADB / 测试','ADB / Tradefed'],'回执 / 日志']],
   ['evidence','实际证据来源','当前工单与匹配版本','search','access',28,302,214,161,['工单 / 附件','日志 / 报告',['Gerrit / 补丁','Gerrit / Mainline / Critical Fix'],['APK / SDK','APK / SDK / CodeSearch']]],
   ['collect','分析 Worker / 取证','Redmine 后台执行','search','access',317,302,193,161,['证据快照',['版本匹配','SDK / Gerrit 同仓完整 commit 关联'],'证据检索',['来源 / 覆盖','统一证据摘要、检索范围与分页缺口']]],
   ['review','诊断推理 / 复核','模型分析与补证','chip','control',592,302,186,161,[['证据门禁','Evidence Gate / 证据门禁'],['结论复核','Claim Review / 结论复核'],'根因 / 不确定性','补证 / 源码验证']],
   ['report','诊断结果 / 状态','结论与建议（非执行）','chart','resource',859,302,214,161,['引用 / 结论','修复建议','验证计划','报告 / run_id']],
   ['owner','授权参考文档','独立 owner 范围','book','external',27,518,216,112,[['PDF / HTML','owner 授权 PDF / HTML'],['模型授权','内容版本及模型主路由 / 回退绑定']]],
   ['sources','外部知识源','只读 / 来源可追溯','book','external',306,518,216,112,[['Wiki','Android Internals Wiki'],['CDD / Web','Android CDD / Web 文档']]],
   ['knowledge','独立 RAG 检索','背景知识 / 版本过滤','search','external',577,518,216,112,['BM25 / 向量 / RRF',['来源 / 覆盖状态','Retrieval：证据引用、覆盖率、有限补证与持久化索引']]],
   ['output','诊断结果消费','读模型 / 会话反馈','workflow','external',858,518,216,112,[['原会话 / 晨报','原会话 / 每日晨报'],['报告 / 知识','报告 / 按需知识沉淀']]]
  ],
  edges:[
   ['M264 132 H389','command','Service Token',325,114],['M731 132 H867','command','按需受控执行',799,114],
   ['M867 198 H731','report','验证回执',795,213],['M463 225 V271 H414 V302','command','校验入队',529,254],
   ['M242 383 H317','data','工单取证',280,366],['M510 383 H592','data','证据包',551,366],['M778 383 H859','report','复核结果',818,366],
   ['M135 518 V490 H367 V463','data','授权召回',220,481],['M414 518 V463','data','规范召回',472,491],
   ['M522 574 H577','data','检索',550,554],['M685 518 V463','data','背景知识',741,491],
   ['M966 463 V518','report','状态 / 结果',1030,491]
  ],
  legend:[['access','用户 / 证据'],['control','Agent 与调度'],['worker','设备执行'],['resource','诊断输出'],['external','知识 / 结果消费']],
  boundary:['Redmine 后台分析 Worker 与设备执行 Worker 独立；设备操作须另行授权。','外部联邦命中为 background；官方规范是要求依据，不单独证明问题根因。','授权 PDF / HTML 按 owner、内容版本及模型目录召回；共享 Partner 扫描仅供人工查询；授权资料不进入公共索引。','run_id 跟踪权威任务状态；恢复跟踪不等于任务完成或取消。'],
  cards:[
   ['诊断链路','access',[['入口','对话 / 晨报 / 工单'],['取证','快照、版本与覆盖'],['召回','混合召回 / 原文引用'],['复核','证据门禁 / Claim Review'],['输出','诊断读模型 / run_id']]],
   ['证据来源','worker',[['Redmine / 日志','正文、回复、附件及日志'],['Gerrit / SDK','变更、APK / 版本源码'],['官方规范','Mainline / CF / CDD'],['知识联邦','Wiki / CDD / Web，background'],['授权资料','owner PDF / HTML、模型路由绑定']]],
   ['结果输出','risk',[['结论','引用、不确定性、证据缺口'],['修复建议','方案 / 补丁候选 / 验证计划'],['设备验证','另行授权 / Worker 回执'],['报告','统一读模型 / 原会话'],['沉淀','按需回写 / 经验知识']]]
  ]
 }
});
