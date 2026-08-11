<script setup lang="ts">
/**
 * OntologyOverviewTab — high-level overview of a semantic model.
 *
 * Shows the description, model-level AI context, a small stat strip
 * (entities / relationships / metrics), an optional validation panel, and an
 * expandable list of entities. Each entity card expands to reveal its
 * attributes, foreign keys, sub-concepts and the relationships it participates
 * in — giving a full structural picture without leaving the tab.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { SemanticModel, OntologyValidationError, Entity } from '@/types/ontology'
import AiContextBlock from './AiContextBlock.vue'

const { t } = useI18n()

const props = defineProps<{
  model: SemanticModel
  validationErrors: OntologyValidationError[]
}>()

const entities = computed<Entity[]>(() =>
  Object.values(props.model.entities ?? {}).sort((a, b) => a.name.localeCompare(b.name)),
)
const relationships = computed(() => Object.values(props.model.relationships ?? {}))
const metricsCount = computed(() => Object.keys(props.model.metrics ?? {}).length)

const stats = computed(() => [
  { key: 'entities', label: t('models.ontology.entities'), value: entities.value.length },
  { key: 'relationships', label: t('models.ontology.relationships'), value: relationships.value.length },
  { key: 'metrics', label: t('models.ontology.metrics'), value: metricsCount.value },
])

// Relationships touching a given entity (either direction), for the entity card.
function relationshipsOf(name: string) {
  return relationships.value
    .filter((r) => r.from_entity === name || r.to_entity === name)
    .map((r) => ({
      name: r.name,
      other: r.from_entity === name ? r.to_entity : r.from_entity,
      direction: r.from_entity === name ? 'out' : 'in',
      multiplicity: r.multiplicity,
    }))
}

// Track which entities are expanded (all collapsed by default).
const expanded = ref<Set<string>>(new Set())
function toggle(name: string) {
  const next = new Set(expanded.value)
  next.has(name) ? next.delete(name) : next.add(name)
  expanded.value = next
}
</script>

<template>
  <div class="overview-tab">
    <!-- Description -->
    <section v-if="model.description" class="block">
      <p class="description">{{ model.description }}</p>
    </section>

    <!-- Stat strip -->
    <section class="stats">
      <div v-for="s in stats" :key="s.key" class="stat">
        <span class="stat-value">{{ s.value }}</span>
        <span class="stat-label">{{ s.label }}</span>
      </div>
    </section>

    <!-- Model-level AI context -->
    <AiContextBlock v-if="model.ai_context" :context="model.ai_context" />

    <!-- Validation panel (errors + warnings) -->
    <section v-if="validationErrors.length" class="validation">
      <h3 class="section-title">
        <t-icon name="error-circle" size="15px" />
        {{ t('models.ontology.validationIssues') }} ({{ validationErrors.length }})
      </h3>
      <ul class="issue-list">
        <li v-for="(err, i) in validationErrors" :key="i" class="issue" :class="err.severity">
          <t-icon
            :name="err.severity === 'error' ? 'error-circle-filled' : 'error-circle'"
            size="13px"
          />
          <div class="issue-body">
            <code class="issue-path">{{ err.path }}</code>
            <span class="issue-msg">{{ err.message }}</span>
          </div>
        </li>
      </ul>
    </section>

    <!-- Entities -->
    <section class="entities">
      <h3 class="section-title">
        <t-icon name="root-list" size="15px" />
        {{ t('models.ontology.entities') }} ({{ entities.length }})
      </h3>

      <div v-if="entities.length === 0" class="empty">{{ t('models.ontology.noEntities') }}</div>

      <div v-for="entity in entities" :key="entity.name" class="entity-card">
        <button class="entity-head" @click="toggle(entity.name)">
          <t-icon
            :name="expanded.has(entity.name) ? 'chevron-down' : 'chevron-right'"
            size="16px"
            class="chevron"
          />
          <span class="entity-name">{{ entity.name }}</span>
          <t-tag
            size="small"
            variant="light"
            :theme="entity.type === 'ValueType' ? 'warning' : 'primary'"
          >
            {{ entity.type }}
          </t-tag>
          <code v-if="entity.model" class="entity-model">{{ entity.model }}</code>
          <span class="entity-meta">
            {{ t('models.ontology.attributesCount', { n: entity.attributes.length }) }}
          </span>
        </button>

        <div v-if="expanded.has(entity.name)" class="entity-body">
          <p v-if="entity.description" class="entity-desc">{{ entity.description }}</p>

          <!-- Key mapping -->
          <div v-if="Object.keys(entity.key).length" class="sub-block">
            <span class="sub-label">{{ t('models.ontology.key') }}</span>
            <div class="tag-row">
              <code v-for="(col, k) in entity.key" :key="k" class="kv">{{ k }} → {{ col }}</code>
            </div>
          </div>

          <!-- Attributes table -->
          <div v-if="entity.attributes.length" class="sub-block">
            <span class="sub-label">{{ t('models.ontology.attributes') }}</span>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>{{ t('models.ontology.attrName') }}</th>
                  <th>{{ t('models.ontology.attrColumn') }}</th>
                  <th>{{ t('models.ontology.attrType') }}</th>
                  <th>{{ t('models.description') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="attr in entity.attributes" :key="attr.name">
                  <td class="mono">{{ attr.name }}</td>
                  <td class="mono muted">{{ attr.column }}</td>
                  <td>
                    <t-tag size="small" variant="outline">{{ attr.type }}</t-tag>
                  </td>
                  <td>{{ attr.description || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Foreign keys -->
          <div v-if="entity.foreign_keys.length" class="sub-block">
            <span class="sub-label">{{ t('models.ontology.foreignKeys') }}</span>
            <div class="tag-row">
              <code v-for="fk in entity.foreign_keys" :key="fk.name" class="kv">
                {{ fk.column }} → {{ fk.references }}
              </code>
            </div>
          </div>

          <!-- Sub-concepts -->
          <div v-if="Object.keys(entity.sub_concepts).length" class="sub-block">
            <span class="sub-label">{{ t('models.ontology.subConcepts') }}</span>
            <div class="tag-row">
              <t-tag
                v-for="sc in entity.sub_concepts"
                :key="sc.name"
                size="small"
                variant="light"
              >
                {{ sc.name }}
              </t-tag>
            </div>
          </div>

          <!-- Relationships -->
          <div v-if="relationshipsOf(entity.name).length" class="sub-block">
            <span class="sub-label">{{ t('models.ontology.relationships') }}</span>
            <div class="rel-row">
              <span
                v-for="rel in relationshipsOf(entity.name)"
                :key="rel.name"
                class="rel-chip"
              >
                <t-icon
                  :name="rel.direction === 'out' ? 'arrow-right' : 'arrow-left'"
                  size="12px"
                />
                {{ rel.other }}
                <span v-if="rel.multiplicity" class="rel-mult">{{ rel.multiplicity }}</span>
              </span>
            </div>
          </div>

          <!-- Entity AI context -->
          <AiContextBlock v-if="entity.ai_context" :context="entity.ai_context" compact />
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.overview-tab {
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.description {
  margin: 0;
  font-size: var(--td-font-size-body-medium);
  line-height: 1.6;
  color: var(--td-text-color-primary);
}

/* ─── Stat strip ─── */
.stats {
  display: flex;
  gap: 12px;
}

.stat {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 12px 16px;
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  background: var(--td-bg-color-container);
}

.stat-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--td-brand-color);
  line-height: 1.1;
}

.stat-label {
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-secondary);
}

/* ─── Section titles ─── */
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--td-font-size-body-medium);
  font-weight: 600;
  color: var(--td-text-color-primary);
  margin: 0 0 10px;
}

/* ─── Validation ─── */
.issue-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.issue {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--td-radius-small);
  font-size: var(--td-font-size-body-small);
}

.issue.error {
  background: var(--td-error-color-1, rgba(215, 63, 63, 0.08));
  color: var(--td-error-color);
}

.issue.warning {
  background: var(--td-warning-color-1, rgba(237, 123, 43, 0.08));
  color: var(--td-warning-color);
}

.issue-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.issue-path {
  font-family: var(--td-font-family-mono);
  font-size: 11px;
  opacity: 0.85;
}

.issue-msg {
  color: var(--td-text-color-primary);
}

/* ─── Entities ─── */
.entity-card {
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  margin-bottom: 8px;
  overflow: hidden;
}

.entity-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: background 0.1s;
}

.entity-head:hover {
  background: var(--td-bg-color-container-hover);
}

.chevron {
  color: var(--td-text-color-secondary);
  flex-shrink: 0;
}

.entity-name {
  font-weight: 600;
  font-size: var(--td-font-size-body-medium);
  color: var(--td-text-color-primary);
}

.entity-model {
  font-family: var(--td-font-family-mono);
  font-size: 11px;
  color: var(--td-text-color-secondary);
  background: var(--td-bg-color-secondarycontainer);
  padding: 1px 6px;
  border-radius: 4px;
}

.entity-meta {
  margin-left: auto;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
  flex-shrink: 0;
}

.entity-body {
  padding: 4px 14px 14px 34px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  border-top: 1px solid var(--td-component-stroke);
}

.entity-desc {
  margin: 10px 0 0;
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-secondary);
  line-height: 1.5;
}

.sub-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sub-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--td-text-color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.tag-row,
.rel-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.kv {
  font-family: var(--td-font-family-mono);
  font-size: 12px;
  padding: 2px 8px;
  background: var(--td-bg-color-secondarycontainer);
  border-radius: 4px;
  color: var(--td-text-color-primary);
}

.rel-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  padding: 2px 8px;
  border: 1px solid var(--td-component-stroke);
  border-radius: 12px;
  color: var(--td-text-color-primary);
}

.rel-mult {
  font-size: 10px;
  color: var(--td-text-color-placeholder);
}

/* ─── Mini table ─── */
.mini-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--td-font-size-body-small);
}

.mini-table th {
  text-align: left;
  font-weight: 600;
  color: var(--td-text-color-secondary);
  padding: 6px 8px;
  border-bottom: 1px solid var(--td-component-stroke);
}

.mini-table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--td-component-stroke);
  color: var(--td-text-color-primary);
  vertical-align: top;
}

.mono {
  font-family: var(--td-font-family-mono);
  font-size: 12px;
}

.muted {
  color: var(--td-text-color-secondary);
}

.empty {
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
  padding: 8px 0;
}
</style>
