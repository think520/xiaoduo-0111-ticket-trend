# 工单字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `ticket_id` | string | 工单唯一编号 |
| `created_at` | string | 创建时间（YYYY-MM-DD HH:MM） |
| `category` | string | 问题分类标签 |
| `description` | string | 问题描述（用户原文） |
| `priority` | string | 优先级（高/中/低） |
| `resolution_time_hours` | number | 处理时长（小时） |
| `satisfaction` | number | 满意度评分（1-5） |
| `channel` | string | 来源渠道（在线/电话/邮件） |
| `is_resolved` | boolean | 是否已解决 |
