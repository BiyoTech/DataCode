<script setup lang="ts">
import { useAuthStore } from '@/stores/auth'
import LoginView from '@/views/LoginView.vue'

const authStore = useAuthStore()
</script>

<template>
  <LoginView v-if="!authStore.isAuthenticated" />
  <RouterView v-else />
</template>
