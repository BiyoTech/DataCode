import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: () => import('@/layouts/AppShell.vue'),
      children: [
        {
          path: '',
          redirect: '/chat',
        },
        {
          path: 'chat',
          name: 'chat',
          component: () => import('@/components/layout/EditorPanel.vue'),
        },
        {
          path: 'plan',
          name: 'plan',
          component: () => import('@/views/PlanView.vue'),
        },
        {
          path: 'models',
          name: 'models',
          component: () => import('@/views/ModelsView.vue'),
          meta: { navOnly: true, hideChat: true },
        },
        {
          path: 'abi',
          name: 'abi',
          component: () => import('@/views/AbiView.vue'),
          meta: { navOnly: true, hideChat: true },
        },
        {
          path: 'asset-dag',
          name: 'asset-dag',
          component: () => import('@/views/AssetDagView.vue'),
          meta: { navOnly: true, hideChat: true },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/SettingsView.vue'),
          meta: { fullPage: true },
        },
      ],
    },
  ],
})

export default router
