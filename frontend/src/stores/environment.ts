/**
 * Environment Store - shared state for the currently selected SQLMesh environment.
 * Used by Toolbar (environment picker) and Plan store (target environment for plan/apply).
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useEnvironmentStore = defineStore('environment', () => {
  const selectedEnvironment = ref('prod')
  const defaultTargetEnvironment = ref('prod')

  const currentEnvironment = computed(() =>
    selectedEnvironment.value || defaultTargetEnvironment.value || 'prod'
  )

  function setEnvironment(env: string): void {
    selectedEnvironment.value = env
  }

  function setDefault(env: string): void {
    defaultTargetEnvironment.value = env
  }

  return {
    selectedEnvironment,
    defaultTargetEnvironment,
    currentEnvironment,
    setEnvironment,
    setDefault,
  }
})
