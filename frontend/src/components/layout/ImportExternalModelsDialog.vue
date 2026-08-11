<script setup lang="ts">
/**
 * ImportExternalModelsDialog — dialect-aware cascade select for external model import.
 *
 * Cascade depth depends on the selected gateway's dialect:
 * - PostgreSQL family (postgres / redshift / greenplum): 3 levels
 *     Database → Schema → Tables
 * - MySQL family (mysql / doris / starrocks / tidb / mariadb) and others: 2 levels
 *     Database → Tables
 *
 * Uses the existing dialog-overlay/dialog style from SidebarPanel.vue
 * for visual consistency.
 */
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useExternalModelsStore } from '@/stores/externalModels'
import { MessagePlugin } from 'tdesign-vue-next'

const { t } = useI18n()

const props = defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  (e: 'update:visible', v: boolean): void
  (e: 'success'): void
}>()

const store = useExternalModelsStore()

// ────── Dialect family detection ──────
const POSTGRES_DIALECTS = ['postgres', 'postgresql', 'redshift', 'greenplum']

function isPostgresFamily(dialect: string): boolean {
  return POSTGRES_DIALECTS.includes(dialect.toLowerCase())
}

// ────── Local form state ──────
const gateway = ref('')
const database = ref('')       // level-1 (both families)
const schema = ref('')         // level-2 (postgres only)
const selectedTables = ref<string[]>([])
const includePattern = ref('')
const excludePattern = ref('')
const strictMode = ref(false)
const sampleCount = ref(3)
const submitting = ref(false)

// ────── Derived: selected gateway + dialect ──────
const selectedGateway = computed(() =>
  store.gateways.find((g) => g.name === gateway.value)
)
const gatewayDialect = computed(() => selectedGateway.value?.dialect || '')
const isThreeLevel = computed(() => isPostgresFamily(gatewayDialect.value))

// ────── Computed select options ──────
// store.schemas holds databases (level-1) or schemas (level-2 after
// fetchSchemasInDatabase). We reuse the same ref for both to keep the
// select binding simple.
const databaseOptions = computed(() => {
  // When isThreeLevel and we haven't picked a database yet, store.schemas
  // holds databases. After picking a database, store.schemas is repopulated
  // with schemas — but databaseOptions should always show databases.
  // We solve this by snapshotting databases before fetching level-2.
  return databaseList.value.map((d) => ({ label: d.name, value: d.name }))
})

const schemaOptions = computed(() =>
  store.schemas.map((s) => ({ label: s.name, value: s.name }))
)

const tableOptions = computed(() =>
  store.tables.map((tbl) => ({ label: tbl.table_name, value: tbl.table_name }))
)

// Snapshot of level-1 databases, kept stable so the Database selector
// doesn't lose its options when store.schemas is repopulated for level-2.
const databaseList = ref<{ name: string }[]>([])

// ────── Watch dialog open ──────
watch(
  () => props.visible,
  (v) => {
    if (v) {
      resetForm()
      store.fetchGateways()
    }
  }
)

// ────── Cascading load on change ──────

// Gateway change → fetch databases (level-1, all dialects)
watch(gateway, (val) => {
  database.value = ''
  schema.value = ''
  selectedTables.value = []
  store.schemas = []
  store.tables = []
  databaseList.value = []
  if (val) {
    store.fetchSchemas(val).then(() => {
      // Snapshot the database list so it survives level-2 repopulation.
      databaseList.value = [...store.schemas]
    })
  }
})

// Database change:
//  - Postgres: fetch schemas within database (level-2)
//  - MySQL/other: fetch tables directly (database acts as schema param)
watch(database, (val) => {
  schema.value = ''
  selectedTables.value = []
  store.tables = []
  if (!val || !gateway.value) return

  if (isThreeLevel.value) {
    // Level-2: fetch schemas within the selected database.
    store.fetchSchemasInDatabase(gateway.value, val)
  } else {
    // 2-level: database IS the schema param for table discovery.
    store.fetchTables(gateway.value, val)
  }
})

// Schema change (postgres only) → fetch tables
watch(schema, (val) => {
  selectedTables.value = []
  store.tables = []
  if (val && gateway.value && isThreeLevel.value) {
    store.fetchTables(gateway.value, val)
  }
})

// ────── Actions ──────
function resetForm() {
  gateway.value = ''
  database.value = ''
  schema.value = ''
  selectedTables.value = []
  includePattern.value = ''
  excludePattern.value = ''
  strictMode.value = false
  sampleCount.value = 3
  submitting.value = false
  databaseList.value = []
}

function close() {
  emit('update:visible', false)
}

async function handleImport() {
  if (!gateway.value || !database.value) {
    MessagePlugin.warning(t('externalModels.import.fillRequired'))
    return
  }
  if (isThreeLevel.value && !schema.value) {
    MessagePlugin.warning(t('externalModels.import.fillRequired'))
    return
  }

  // For the backend import API, "schema" param = actual schema (postgres 3-level)
  // or database name (mysql 2-level).
  const schemaParam = isThreeLevel.value ? schema.value : database.value

  submitting.value = true
  try {
    const result = await store.importModels({
      gateway: gateway.value,
      schema: schemaParam,
      tables: selectedTables.value,
      include: includePattern.value,
      exclude: excludePattern.value,
      samples: sampleCount.value,
      strict: strictMode.value,
    })

    const msg = t('externalModels.import.success', { count: result.success_count })
    MessagePlugin.success(msg)
    emit('success')
    close()
  } catch (e: any) {
    // Error already handled in store — show toast from store.importError
    const detail = store.importError || String(e)
    MessagePlugin.error(t('externalModels.import.error', { message: detail }))
  } finally {
    submitting.value = false
  }
}

function toggleAllTables() {
  if (selectedTables.value.length === store.tables.length) {
    selectedTables.value = []
  } else {
    selectedTables.value = store.tables.map((tbl) => tbl.table_name)
  }
}

// Whether the tables selector is enabled (depends on cascade level).
const tablesDisabled = computed(() =>
  isThreeLevel.value ? !schema.value : !database.value
)

const gatewayOptions = computed(() =>
  store.gateways.map((g) => {
    const label = g.is_default ? `${g.name} (${t('common.default')})` : g.name
    return { label, value: g.name }
  })
)
</script>

<template>
  <div v-if="visible" class="dialog-overlay" @click.self="close">
    <div class="dialog" style="width: 480px">
      <h3>{{ t('externalModels.import.title') }}</h3>

      <!-- Gateway -->
      <div class="dialog-field">
        <label>{{ t('externalModels.import.gateway') }}</label>
        <t-select
          v-model="gateway"
          :options="gatewayOptions"
          :loading="store.gatewaysLoading"
          :placeholder="t('common.select') + '...'"
          filterable
        />
      </div>

      <!-- Level-1: Database (all dialects) -->
      <div class="dialog-field">
        <label>{{ t('externalModels.import.database') }}</label>
        <t-select
          v-model="database"
          :options="databaseOptions"
          :loading="store.schemasLoading"
          :disabled="!gateway"
          :placeholder="t('common.select') + '...'"
          filterable
        />
      </div>

      <!-- Level-2: Schema (postgres family only) -->
      <div v-if="isThreeLevel" class="dialog-field">
        <label>{{ t('externalModels.import.schema') }}</label>
        <t-select
          v-model="schema"
          :options="schemaOptions"
          :loading="store.schemasLoading"
          :disabled="!database"
          :placeholder="t('common.select') + '...'"
          filterable
        />
      </div>

      <!-- Tables (multi-select with toggle-all) -->
      <div class="dialog-field">
        <label>
          {{ t('externalModels.import.tables') }}
          <button
            v-if="!tablesDisabled && store.tables.length > 0"
            class="toggle-all-btn"
            @click="toggleAllTables"
            type="button"
          >
            {{ selectedTables.length === store.tables.length ? t('externalModels.import.deselectAll') : t('externalModels.import.selectAll') }}
          </button>
        </label>
        <t-select
          v-model="selectedTables"
          :options="tableOptions"
          :loading="store.tablesLoading"
          :disabled="tablesDisabled"
          :placeholder="t('externalModels.import.tablesHint')"
          multiple
          clearable
          filterable
        />
      </div>

      <!-- Include / Exclude patterns -->
      <div style="display: flex; gap: 8px">
        <div class="dialog-field" style="flex: 1">
          <label>{{ t('externalModels.import.include') }}</label>
          <t-input v-model="includePattern" :placeholder="'orders,user_*'" />
        </div>
        <div class="dialog-field" style="flex: 1">
          <label>{{ t('externalModels.import.exclude') }}</label>
          <t-input v-model="excludePattern" :placeholder="'tmp_*,bak_*'" />
        </div>
      </div>

      <!-- Samples & Strict -->
      <div style="display: flex; gap: 8px; align-items: flex-end">
        <div class="dialog-field" style="flex: 1">
          <label>{{ t('externalModels.import.samples') }}</label>
          <t-input-number v-model="sampleCount" :min="0" :max="20" />
        </div>
        <div class="dialog-field" style="flex: 1; flex-direction: row; align-items: center; gap: 8px">
          <t-switch v-model="strictMode" size="small" />
          <label style="margin: 0">{{ t('externalModels.import.strict') }}</label>
          <t-tooltip :content="t('externalModels.import.strictHint')" placement="top">
            <t-icon name="info-circle" size="14px" class="info-icon" />
          </t-tooltip>
        </div>
      </div>

      <!-- Actions -->
      <div class="dialog-actions">
        <button class="btn-cancel" @click="close">{{ t('common.cancel') }}</button>
        <button
          class="btn-primary"
          :disabled="!gateway || !database || (isThreeLevel && !schema) || submitting"
          @click="handleImport"
        >
          <t-icon v-if="submitting" name="loading" size="14px" style="margin-right: 4px" />
          {{ submitting ? t('common.loading') : t('externalModels.import.button') }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* dialog-overlay / dialog reuse same style as SidebarPanel.vue */
.dialog-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.dialog {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
}

.dialog h3 {
  font-size: 16px;
  margin-bottom: 16px;
  color: var(--text-primary);
}

.dialog-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}

.dialog-field label {
  font-size: 12px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.toggle-all-btn {
  font-size: 11px;
  color: var(--accent);
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
  text-decoration: underline;
}

.toggle-all-btn:hover {
  color: var(--accent-hover);
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.btn-cancel {
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 13px;
}

.btn-cancel:hover {
  background: var(--bg-hover);
}

.btn-primary {
  padding: 6px 14px;
  border: none;
  border-radius: 6px;
  background: var(--accent);
  color: #fff;
  cursor: pointer;
  font-size: 13px;
  display: flex;
  align-items: center;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary:not(:disabled):hover {
  filter: brightness(1.1);
}

.info-icon {
  color: var(--text-muted);
  cursor: help;
  transition: color 0.15s;
}

.info-icon:hover {
  color: var(--text-secondary);
}
</style>
