import type { RouteRecordRaw } from 'vue-router'

export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/login'
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue')
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue')
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('@/views/ChatView.vue')
  },
  {
    path: '/chat/:conversationId',
    name: 'chat-detail',
    component: () => import('@/views/ChatView.vue')
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue')
  },
  {
    path: '/library',
    name: 'library',
    component: () => import('@/views/LibraryView.vue')
  },
  {
    path: '/projects',
    name: 'projects',
    component: () => import('@/views/ProjectListView.vue')
  },
  {
    path: '/projects/:projectId',
    name: 'projects-detail',
    component: () => import('@/views/ProjectDetailView.vue')
  },
  {
    path: '/templates',
    name: 'templates',
    component: () => import('@/views/TemplateMarketView.vue')
  },
  {
    path: '/templates/mine/:userTemplateId/preview',
    name: 'my-template-preview',
    component: () => import('@/views/DocumentPreviewView.vue')
  },
  {
    path: '/templates/:templateId/preview',
    name: 'template-preview',
    component: () => import('@/views/DocumentPreviewView.vue')
  },
  {
    path: '/help',
    name: 'help',
    component: () => import('@/views/help/HelpHomeView.vue')
  },
  {
    path: '/help/article/:slug',
    name: 'help-article',
    component: () => import('@/views/help/HelpArticleView.vue')
  },
  {
    path: '/library/items/:itemId/preview',
    name: 'document-preview',
    component: () => import('@/views/DocumentPreviewView.vue')
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/chat'
  }
]
