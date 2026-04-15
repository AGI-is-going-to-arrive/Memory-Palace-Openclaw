> [English](07-PHASED_UPGRADE_ROADMAP.en.md)

# 07 · 分阶段升级路线图（归档回顾）

这页现在只回答一件事：

> 这条升级路线历史上经过了哪些阶段，以及今天还剩哪些维护边界。

它现在是一页归档回顾，不再承担新增主线范围定义的作用。

---

## 1. 当前怎么读这页

当前更准确的读法是：

- `Phase 0-10` 代表历史主线推进过程
- 当前重点不是继续扩主功能
- 当前重点是目标环境复跑、平台验证和新能力接入时补 gate

如果你只想看当前用户该怎么装、怎么用、怎么验证：

- 不需要先看这页
- 直接看 `README.md`、`01-INSTALL_AND_RUN.md`、`15-END_USER_INSTALL_AND_USAGE.md`、`docs/EVALUATION.md`

---

## 2. 当前主链阶段回顾

这条路线现在更适合当成归档索引来读：

1. `Phase 0-2`
   - 文档分层、安装入口、`setup / verify / doctor / smoke / migrate / upgrade` 已落地
2. `Phase 3-5`
   - recall / capture 生命周期、多 Agent ACL、命名空间治理已落地
3. `Phase 6-8`
   - reflection lane、检索排序与召回、visual memory 主链已落地
4. `Phase 9-10`
   - observability、Dashboard、发布门与回滚命令面已落地

对今天的维护工作来说，更重要的是：

- 当前已经不是“靠路线图决定主线范围”的阶段
- 当前更像“主功能已落地，后续靠复跑、验证和维护纪律收敛风险”

---

## 3. 当前仍要保守写的只有什么

现在还要保守写的，主要只剩两类：

- 目标环境复跑
- 新增能力族时同步补测试、smoke、benchmark、gate

这两类边界不等于“主链还没完成”，而是当前维护阶段的正常成本。

---

## 4. 这页和其它维护者文档的分工

如果你还需要继续看维护资料，建议这样分：

- `00-IMPLEMENTED_CAPABILITIES.md`
  - 看当前已经稳定成立的事实
- `06-UPGRADE_CHECKLIST.md`
  - 看发布前还该怎么复核
- `docs/EVALUATION.md`
  - 看当前公开验证基线
- `docs/TECHNICAL_OVERVIEW.md`
  - 看实现结构

---

## 一句总结

> **这页现在只是归档回顾索引页。今天的重点是复跑、验证和维护，而不是继续用阶段路线图定义公开主叙事。**
