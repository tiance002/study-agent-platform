# 发现

- 初始 HEAD b0afdb2，工作区无已跟踪修改。
- 附件推荐 Dify 外置编排，但需按当前实现而非历史状态重评。
- 9/25 已完成认证重建、前端学习闭环、知识库、模板计划生成、数据库单基线；历史报告全量 986 passed / 1 skipped，尚需本轮验证。
- 现有快速交付政策冻结无测量依据的大型架构引入。
- 当前近期审查范围 c28817e..b0afdb2：117文件，11949插入/8859删除，包含vendor与迁移重写，不能用行数估产品完成率。
- 已核实：模板计划 template/graph-v2；teaching provider 正式适配器已存在；Context含历史+检索，尚无项目状态/长期语义记忆完整注入；memory_store 指内存仓储，并非用户长期记忆。
- 真实语义embedding/pgvector未装配，hashing向量仅用于测试；最终教学答案固定cloud，本地模型仅查询改写，不是完整L0/L1/L2。
- Dify官方当前支持外部retrieval API，但共享Bearer key及metadata不能代替最终用户授权；Docker官方最低2核4GiB，多服务部署。Langflow支持Docker与Python组件，同样不免除现有业务适配。
- 初选现有代码路线优先交付；Dify试验放首版之后，以可测节省和等价验收决定。
- 实际复现：已有同URL同标题selected候选时，知识库关联走复用旧候选，但新library-attach幂等键不匹配，API返回403 ILLEGAL_STATE_TRANSITION（library_routes.py 346；memory_acquisition_store.py状态门）。
- Chrome与Edge实测：密码输入框maxlength=12个UTF-16码元，7个emoji仅录入6个；后端接受6–12 Unicode码点。前端views.js:78与服务端契约不一致。
- 前端views.js:301 `status !== pending`使done/skipped任务仍显示自报按钮；服务端只接受in_progress，导致用户操作被拒。
- 库内新版关联：测试明确断言重复关联后响应报告版本2但项目仍是版本1；library.py文档又说重新关联可拿新版，需统一产品契约。当前UI不能用重复关联刷新资料。
- 前端knowledge列表刷新 guard 把整个scope epoch（含项目与会话切换）当成账号边界；请求未完成时切项目可能丢弃结果且账号未变不会重试。
