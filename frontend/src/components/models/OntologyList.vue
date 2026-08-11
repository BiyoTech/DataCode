<script setup lang="ts">
/**
 * OntologyList — flat list of user-authored ontologies for the sidebar.
 *
 * Each row shows the ontology name (file stem) and a brief description
 * extracted from the YAML's top-level `description` field. Clicking a row
 * emits a `select` event so the parent (ModelsView) can render the detail
 * panel.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useOntologyStore } from '@/stores/ontology'

const { t } = useI18n()
const ontologyStore = useOntologyStore()

const emit = defineEmits<{
  (e: 'select', name: string): void
}>()

// Apply the active search query to the ontology list.
const visibleOntologies = computed(() => {
  const q = props.searchQuery.trim().toLowerCase()
  if (!q) return ontologyStore.ontologies
  return ontologyStore.ontologies.filter(
    (o) => o.name.toLowerCase().includes(q) || o.path.toLowerCase().includes(q)
  )
})

const props = defineProps<{
  searchQuery: string
}>()

function handleSelect(name: string) {
  emit('select', name)
}
</script>

<template>
  <div class="ontology-list">
    <div v-if="ontologyStore.isLoading" class="ontology-loading">
      <t-icon name="loading" size="16px" class="spinning" />
      <span>{{ t('common.loading') }}</span>
    </div>

    <template v-else>
      <div
        v-for="ontology in visibleOntologies"
        :key="ontology.name"
        class="ontology-item"
        :class="{ selected: ontologyStore.selectedOntologyName === ontology.name }"
        @click="handleSelect(ontology.name)"
      >
        <t-icon name="root-list" size="14px" class="item-icon" />
        <div class="item-body">
          <span class="item-name">{{ ontology.name }}</span>
          <span class="item-path">{{ ontology.path }}</span>
        </div>
      </div>

      <div
        v-if="visibleOntologies.length === 0"
        class="ontology-empty"
      >
        {{ t('models.ontology.empty') }}
      </div>
    </template>
  </div>
</template>

<style scoped>
.ontology-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
  scrollbar-width: thin;
}

.ontology-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.ontology-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-primary);
  transition: background 0.1s;
  user-select: none;
}

.ontology-item:hover {
  background: var(--td-bg-color-container-hover);
}

.ontology-item.selected {
  background: var(--td-brand-color-light);
  color: var(--td-brand-color);
}

.ontology-item.selected .item-icon {
  color: var(--td-brand-color);
}

.item-icon {
  flex-shrink: 0;
  color: var(--td-text-color-secondary);
}

.item-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.item-name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item-path {
  font-size: 11px;
  color: var(--td-text-color-placeholder);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ontology-empty {
  padding: 16px;
  text-align: center;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
}
</style>
