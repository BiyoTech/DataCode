<script setup lang="ts">
/**
 * AbiDashboard — dashboards built from A-BI analysis results.
 * Framework skeleton only; visualization wiring is added later.
 */
import { useI18n } from 'vue-i18n'
import AbiPlaceholder from './AbiPlaceholder.vue'

const { t } = useI18n()
</script>

<template>
  <AbiPlaceholder icon="dashboard" :title="t('abi.nav.dashboard')" />
</template>
