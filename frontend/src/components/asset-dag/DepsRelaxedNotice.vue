<script setup lang="ts">
/**
 * DepsRelaxedNotice — platform packages this working copy now overrides.
 *
 * Not a failure, which is why it is a notice and not a card: the install
 * succeeded. What it reports is a standing divergence. The platform pins its
 * own package versions so a workspace overlay cannot replace them, but pinning
 * ALL of them would give several hundred packages a veto over every
 * declaration — so a pin whose replacement is local to one copy (no compiled
 * extension, not part of the platform's own runtime) is released when keeping
 * it would have made the declaration unsatisfiable.
 *
 * The reason it is shown rather than only logged: from here on, this copy's
 * code location and sandboxes import a different version than the platform's
 * own processes do. Six months later, "why does only this workspace behave
 * like that" has to have a visible answer.
 */
import { useI18n } from 'vue-i18n'
import type { DepsRelaxedPin } from '@/types/dagster'

defineProps<{ relaxed: DepsRelaxedPin[] }>()

const { t } = useI18n()
</script>

<template>
  <div class="deps-relaxed">
    <div class="relaxed-head">
      <t-icon name="info-circle" size="15px" />
      <span class="relaxed-title">{{ t('deploy.depsRelaxed.title') }}</span>
    </div>

    <p class="relaxed-explain">{{ t('deploy.depsRelaxed.explain') }}</p>

    <ul class="relaxed-list">
      <li v-for="item in relaxed" :key="item.name">
        <code>{{ item.name }}</code>
        <span class="relaxed-versions">
          {{ t('deploy.depsRelaxed.override', {
            platform: item.platform_version,
            overlay: item.overlay_version,
          }) }}
        </span>
        <span v-if="item.required_by" class="relaxed-asker">
          {{ t('deploy.depsRelaxed.requiredBy', { via: item.required_by }) }}
        </span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.deps-relaxed {
  border: 1px solid var(--td-warning-color-3, #f9d39b);
  background: var(--td-warning-color-1, #fff8f0);
  border-radius: 6px;
  padding: 10px 12px;
  margin: 8px 0;
  font-size: 12px;
}

.relaxed-head {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--td-warning-color, #e37318);
}

.relaxed-title {
  font-weight: 600;
}

.relaxed-explain {
  margin: 6px 0 8px;
  color: var(--td-text-color-secondary, #666);
  line-height: 1.5;
}

.relaxed-list {
  margin: 0;
  padding-left: 16px;
  line-height: 1.7;
}

.relaxed-list code {
  font-family: var(--code-font, monospace);
}

.relaxed-versions {
  margin-left: 6px;
  color: var(--td-text-color-secondary, #666);
}

.relaxed-asker {
  margin-left: 6px;
  color: var(--td-text-color-placeholder, #999);
}
</style>
