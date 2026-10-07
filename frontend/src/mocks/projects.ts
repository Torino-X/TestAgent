import type { ProjectPreview } from '@/types/project'

export const mockProjects: ProjectPreview[] = [
  {
    id: 'prj_video_generation',
    name: '视频生成',
    description: '视频生成产品的需求、接口与回归测试工作区。',
    updatedAt: '星期日',
    createdByCurrentUser: true,
    pinned: true,
    memoryMode: 'project_memory',
    conversations: [
      { id: 'conv_video_api', title: '视频生成接口测试方案', summary: '围绕任务提交、状态查询、失败重试和素材校验梳理测试范围。', updatedAt: '今天' },
      { id: 'conv_video_review', title: 'PRD 风险审查', summary: '已整理首轮需求中的边界条件与待确认项。', updatedAt: '昨天' }
    ],
    sources: [],
    artifacts: [
      { id: 'art_video_plan', name: '视频生成接口测试方案.docx', type: '测试方案', updatedAt: '星期日' }
    ]
  },
  {
    id: 'prj_testagent',
    name: 'TestAgent',
    description: 'TestAgent 的产品迭代与验证工作区。',
    updatedAt: '8月4日',
    createdByCurrentUser: true,
    memoryMode: 'project_memory',
    conversations: [
      { id: 'conv_project_requirements', title: 'Project 功能需求梳理', summary: '梳理工作区、资料、记忆和测试资产的产品边界。', updatedAt: '8月4日' }
    ],
    sources: [],
    artifacts: [
      { id: 'art_project_review', name: 'Project 架构审计报告.md', type: '审查报告', updatedAt: '8月4日' }
    ]
  },
  {
    id: 'prj_account_center',
    name: '账号中心',
    description: '账号与权限模块的测试资料和会话。',
    updatedAt: '7月28日',
    createdByCurrentUser: true,
    memoryMode: 'none',
    conversations: [],
    sources: [],
    artifacts: []
  }
]
