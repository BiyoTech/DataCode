<script setup lang="ts">
/**
 * AssetListPanel — left glassmorphism panel listing workspace assets.
 *
 * Assets are grouped by their group name (falling back to type) and filtered by
 * a fuzzy search box. Clicking an asset selects it in the shared Dagster store,
 * which highlights the corresponding DAG node and opens the detail panel.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDagsterStore } from '@/stores/dagster'
import { resolveAssetColor } from './assetDagNodeRegister'
import type { AssetInfo } from '@/types/dagster'

const { t } = useI18n()
const dagster = useDagsterStore()

const search = ref('')

// Display name for an asset: prefer the SQLMesh model name (`schema.table`,
// matching /api/v1/sqlmesh/models) when present, otherwise the asset key string.
function displayName(a: AssetInfo): string {
  return a.name || a.asset_key_str
}

// Group filtered assets by group_name (or kind as fallback).
const groups = computed<Array<{ name: string; assets: AssetInfo[] }>>(() => {
  const q = search.value.trim().toLowerCase()
  const filtered = dagster.graph.nodes.filter((a) =>
    q ? displayName(a).toLowerCase().includes(q) : true,
  )
  const map = new Map<string, AssetInfo[]>()
  for (const a of filtered) {
    const key = a.group_name || a.kind || 'default'
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(a)
  }
  return Array.from(map.entries())
    .map(([name, assets]) => ({
      name,
      assets: assets.sort((x, y) => displayName(x).localeCompare(displayName(y))),
    }))
    .sort((a, b) => a.name.localeCompare(b.name))
})

function select(key: string) {
  dagster.selectAsset(key)
}
</script>

<template>
  <div class="asset-list">
    <div class="list-header">
      <span class="list-title">{{ t('assetDag.assets') }}</span>
      <span class="list-count">{{ dagster.graph.nodes.length }}</span>
    </div>

    <div class="list-search">
      <t-icon name="search" size="14px" class="search-icon" />
      <input v-model="search" class="search-input" :placeholder="t('assetDag.searchAssets')" />
    </div>

    <div class="list-body">
      <div v-if="groups.length === 0" class="list-empty">
        {{ t('assetDag.noAssets') }}
      </div>

      <div v-for="group in groups" :key="group.name" class="asset-group">
        <div class="group-label">
          <t-icon name="folder" size="12px" />
          <span>{{ group.name }}</span>
          <span class="group-count">{{ group.assets.length }}</span>
        </div>
        <button
          v-for="asset in group.assets"
          :key="asset.asset_key_str"
          class="asset-item"
          :class="{ active: dagster.selectedAssetKey === asset.asset_key_str }"
          @click="select(asset.asset_key_str)"
        >
          <span class="type-bar" :style="{ background: resolveAssetColor(asset.kind || 'data') }" />
          <span class="asset-name">{{ displayName(asset) }}</span>
          <span v-if="asset.deps.length" class="asset-deps">↑{{ asset.deps.length }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.asset-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border);
}

.list-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 16px 8px;
  flex-shrink: 0;
}
.list-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.list-count {
  padding: 0 8px;
  border-radius: 10px;
  background: var(--accent-subtler);
  color: var(--accent);
  font-size: 11px;
  font-weight: 500;
}

.list-search {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 12px 10px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--input-bg);
}
.search-icon {
  color: var(--text-muted);
}
.search-input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
}
.search-input::placeholder {
  color: var(--text-muted);
}

.list-body {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px 12px;
}

.list-empty {
  padding: 24px 12px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
}

.asset-group {
  margin-bottom: 10px;
}
.group-label {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.group-count {
  color: var(--text-muted);
  font-weight: 400;
}

.asset-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 8px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  text-align: left;
  transition: all var(--transition-base);
}
.asset-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.asset-item.active {
  background: var(--accent-subtle);
  color: var(--accent);
}
.type-bar {
  width: 3px;
  height: 16px;
  border-radius: 2px;
  flex-shrink: 0;
}
.asset-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.asset-deps {
  color: var(--text-muted);
  font-size: 11px;
  flex-shrink: 0;
}
</style>
