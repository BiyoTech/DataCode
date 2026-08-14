import { createRouter, createWebHistory } from 'vue-router'

import { getSession } from '@/utils/authSession'
import { setUnauthenticatedHandler } from '@/utils/api'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
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

/**
 * Authentication guard. Routes are protected by default; only routes flagged
 * `meta.public` are reachable without a session. The original target is kept
 * in `redirect` so the user lands where they intended after signing in.
 */
router.beforeEach((to) => {
  const isAuthenticated = getSession() !== null

  if (!to.meta.public && !isAuthenticated) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }
  if (to.name === 'login' && isAuthenticated) {
    return { path: '/' }
  }
  return true
})

// Send the user back to the login page when a session cannot be refreshed.
setUnauthenticatedHandler(() => {
  const current = router.currentRoute.value
  if (current.name === 'login') return

  router.replace({
    name: 'login',
    query: current.fullPath === '/' ? {} : { redirect: current.fullPath },
  })
})

export default router
