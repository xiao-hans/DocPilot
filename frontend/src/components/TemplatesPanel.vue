<script setup lang="ts">
/**
 * 模板管理：草稿字段编辑 + 发布不可变版本 + 复制与停用。
 * 发布后版本不可改（后端约束），界面只允许编辑草稿字段；被任务引用过的版本不能删除，
 * 因此这里不提供删除入口，只提供"停用"。
 */

import { computed, onMounted, ref } from 'vue'
import {
  copyTemplate,
  createTemplate,
  getTemplate,
  listTemplates,
  publishTemplate,
  retireTemplate,
  updateTemplate,
  type TemplateDto,
  type TemplateField,
  type TemplateStatus,
} from '../api/admin'
import { ApiError } from '../api/client'

const props = defineProps<{ role: string }>()

const templates = ref<TemplateDto[]>([])
const filter = ref<'' | TemplateStatus>('')
const selected = ref<TemplateDto | null>(null)
const draft = ref<TemplateField[]>([])
const draftName = ref('')
const creating = ref(false)
const createName = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')

const FIELD_TYPES = ['text', 'number', 'date']
const statusLabels: Record<string, string> = {
  draft: '草稿',
  published: '已发布',
  retired: '已停用',
}

const canManage = computed(() => ['admin', 'owner'].includes(props.role))
const dirty = computed(
  () =>
    !!selected.value &&
    (draftName.value !== selected.value.name ||
      JSON.stringify(draft.value) !== JSON.stringify(selected.value.draft_fields)),
)

async function load(keepSelection = true) {
  busy.value = true
  error.value = ''
  try {
    templates.value = await listTemplates(filter.value || undefined)
    const current = keepSelection ? selected.value : null
    if (current) {
      const stillThere = templates.value.find((item) => item.id === current.id)
      await (stillThere ? select(stillThere.id) : clearSelection())
    }
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '模板列表加载失败'
  } finally {
    busy.value = false
  }
}

async function select(templateId: string) {
  error.value = ''
  notice.value = ''
  try {
    const detail = await getTemplate(templateId)
    selected.value = detail
    draftName.value = detail.name
    draft.value = detail.draft_fields.map((field) => ({ ...field }))
    creating.value = false
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '模板详情加载失败'
  }
}

async function clearSelection() {
  selected.value = null
  draft.value = []
  draftName.value = ''
}

function startCreate() {
  selected.value = null
  creating.value = true
  createName.value = ''
  draft.value = [{ name: '', type: 'text', description: '' }]
  notice.value = ''
}

function addField() {
  draft.value = [...draft.value, { name: '', type: 'text', description: '' }]
}

function dropField(index: number) {
  draft.value = draft.value.filter((_, position) => position !== index)
}

function usableFields(): TemplateField[] | null {
  const filled = draft.value.filter((field) => field.name.trim() && field.description?.trim())
  if (!filled.length) {
    error.value = '至少需要一个字段，且名称与说明都不能为空'
    return null
  }
  if (filled.length !== draft.value.length) {
    error.value = '有字段缺名称或说明：说明是提取准确度的关键，不能留空'
    return null
  }
  error.value = ''
  return filled.map((field) => ({ ...field, name: field.name.trim() }))
}

async function saveDraft() {
  const fields = usableFields()
  if (!fields) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    if (creating.value) {
      if (!createName.value.trim()) {
        error.value = '请填写模板名称'
        return
      }
      const created = await createTemplate(createName.value.trim(), fields)
      creating.value = false
      await load()
      await select(created.id)
      // select() 会清掉提示，成功文案必须排在它之后，否则"已创建为草稿"永远看不见。
      notice.value = `模板「${created.name}」已创建为草稿`
      return
    }
    if (!selected.value) return
    const saved = await updateTemplate(selected.value.id, { name: draftName.value.trim(), fields })
    await load()
    await select(saved.id)
    notice.value = '草稿字段已保存'
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '草稿保存失败'
  } finally {
    busy.value = false
  }
}

async function act(label: string, run: () => Promise<unknown>) {
  if (!selected.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await run()
    await load()
    await select(selected.value.id)
    // select() 会清提示：成功文案写在它后面，否则"已发布/已复制"一闪即被抹掉。
    notice.value = label
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '操作失败'
  } finally {
    busy.value = false
  }
}

const publish = () => act('已发布为新版本，之后任务可绑定该版本', () => publishTemplate(selected.value!.id))
const retire = () => act('模板已停用，不再出现在可选列表', () => retireTemplate(selected.value!.id))
const duplicate = () =>
  act('已复制为新的草稿模板', async () => {
    const copy = await copyTemplate(selected.value!.id)
    await load(false)
    await select(copy.id)
  })

onMounted(load)
</script>

<template>
  <section class="space-y-6" aria-label="模板管理">
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h2 class="font-display text-xl font-semibold text-primary">提取模板</h2>
        <p class="mt-1 text-xs text-muted">
          草稿可以反复改；发布生成不可变版本，任务绑定的是版本，因此历史结果不会被后续修改影响。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <label class="sr-only" for="template-filter">按状态筛选</label>
        <select
          id="template-filter"
          v-model="filter"
          class="rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
          @change="load(false)"
        >
          <option value="">全部状态</option>
          <option value="draft">草稿</option>
          <option value="published">已发布</option>
          <option value="retired">已停用</option>
        </select>
        <button
          v-if="canManage"
          class="rounded-control bg-brand px-4 py-2 text-xs font-medium text-on-brand transition-colors hover:bg-brand-soft"
          type="button"
          @click="startCreate"
        >
          新建模板
        </button>
      </div>
    </div>

    <p v-if="error" class="rounded-card border border-danger/40 bg-danger/10 px-4 py-3 text-xs text-danger" role="alert">
      {{ error }}
    </p>
    <p v-if="notice" class="rounded-card border border-edge bg-surface-2 px-4 py-3 text-xs text-secondary" role="status">
      {{ notice }}
    </p>

    <div class="grid gap-6 lg:grid-cols-[280px_1fr]">
      <ul class="space-y-2" aria-label="模板列表">
        <li v-if="!templates.length" class="text-xs text-muted">当前没有模板。</li>
        <li v-for="item in templates" :key="item.id">
          <button
            class="w-full rounded-card border px-4 py-3 text-left transition-colors"
            :class="selected?.id === item.id ? 'border-brand bg-surface-2' : 'border-edge bg-surface hover:border-brand'"
            type="button"
            @click="select(item.id)"
          >
            <span class="block text-xs text-primary">{{ item.name }}</span>
            <span class="mt-1 block font-mono text-xs text-muted tabular-nums">
              {{ statusLabels[item.status] ?? item.status }}
              <template v-if="item.latest_revision"> · v{{ item.latest_revision }}</template>
              · {{ item.draft_fields.length }} 个字段
            </span>
          </button>
        </li>
      </ul>

      <div v-if="creating" class="space-y-4 rounded-card border border-edge bg-surface px-5 py-4">
        <p class="text-xs font-semibold text-primary">新建模板</p>
        <label class="block space-y-1">
          <span class="text-xs text-secondary">模板名称</span>
          <input
            v-model="createName"
            class="w-full rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
            placeholder="如：增值税专用发票"
            type="text"
          />
        </label>
        <ul class="space-y-2">
          <li v-for="(field, index) in draft" :key="index" class="flex flex-wrap items-center gap-2">
            <input
              v-model="field.name"
              class="w-[130px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
              placeholder="字段名"
              type="text"
            />
            <select
              v-model="field.type"
              class="rounded-control border border-edge bg-surface px-2 py-2 text-xs text-primary"
              aria-label="字段类型"
            >
              <option v-for="type in FIELD_TYPES" :key="type" :value="type">{{ type }}</option>
            </select>
            <input
              v-model="field.description"
              class="min-w-[200px] flex-1 rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
              placeholder="说明：告诉模型这个字段在哪里、长什么样"
              type="text"
            />
            <button
              class="rounded-control border border-edge px-3 py-2 text-xs text-secondary transition-colors hover:border-danger hover:text-danger"
              type="button"
              @click="dropField(index)"
            >
              删除
            </button>
          </li>
        </ul>
        <div class="flex items-center gap-2">
          <button
            class="rounded-control border border-edge px-3 py-2 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
            type="button"
            @click="addField"
          >
            加一个字段
          </button>
          <button
            class="ml-auto rounded-control bg-brand px-4 py-2 text-xs font-medium text-on-brand transition-colors hover:bg-brand-soft"
            type="button"
            :disabled="busy"
            @click="saveDraft"
          >
            创建草稿
          </button>
        </div>
      </div>

      <div v-else-if="selected" class="space-y-4 rounded-card border border-edge bg-surface px-5 py-4">
        <div class="flex flex-wrap items-center gap-3">
          <input
            v-model="draftName"
            class="w-[240px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
            aria-label="模板名称"
            :disabled="!canManage"
            type="text"
          />
          <span class="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-xs text-secondary">
            {{ statusLabels[selected.status] ?? selected.status }}
          </span>
          <span v-if="canManage" class="ml-auto flex items-center gap-2">
            <button
              class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
              type="button"
              :disabled="busy || !dirty"
              @click="saveDraft"
            >
              保存草稿
            </button>
            <button
              class="rounded-control bg-brand px-3 py-1.5 text-xs font-medium text-on-brand transition-colors hover:bg-brand-soft"
              type="button"
              :disabled="busy"
              @click="publish"
            >
              发布新版本
            </button>
            <button
              class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
              type="button"
              :disabled="busy"
              @click="duplicate"
            >
              复制为草稿
            </button>
            <button
              v-if="selected.status !== 'retired'"
              class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary transition-colors hover:border-danger hover:text-danger"
              type="button"
              :disabled="busy"
              @click="retire"
            >
              停用
            </button>
          </span>
        </div>

        <ul class="space-y-2">
          <li v-for="(field, index) in draft" :key="index" class="flex flex-wrap items-center gap-2">
            <input
              v-model="field.name"
              class="w-[130px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
              aria-label="字段名"
              :disabled="!canManage"
              type="text"
            />
            <select
              v-model="field.type"
              class="rounded-control border border-edge bg-surface px-2 py-2 text-xs text-primary"
              aria-label="字段类型"
              :disabled="!canManage"
            >
              <option v-for="type in FIELD_TYPES" :key="type" :value="type">{{ type }}</option>
            </select>
            <input
              v-model="field.description"
              class="min-w-[200px] flex-1 rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
              aria-label="字段说明"
              :disabled="!canManage"
              type="text"
            />
            <button
              v-if="canManage"
              class="rounded-control border border-edge px-3 py-2 text-xs text-secondary transition-colors hover:border-danger hover:text-danger"
              type="button"
              @click="dropField(index)"
            >
              删除
            </button>
          </li>
        </ul>
        <button
          v-if="canManage"
          class="rounded-control border border-edge px-3 py-2 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
          type="button"
          @click="addField"
        >
          加一个字段
        </button>

        <div class="space-y-2 border-t border-edge pt-4">
          <p class="text-xs font-semibold text-primary">已发布版本（{{ selected.versions?.length ?? 0 }}）</p>
          <p v-if="!selected.versions?.length" class="text-xs text-muted">还没有发布过版本，任务暂时无法绑定这个模板。</p>
          <ul class="space-y-1">
            <li v-for="version in selected.versions" :key="version.id" class="flex items-center gap-3">
              <span class="font-mono text-xs text-secondary tabular-nums">v{{ version.revision }}</span>
              <span class="text-xs text-muted">{{ version.fields.length }} 个字段</span>
              <span class="font-mono text-xs text-muted tabular-nums">
                {{ version.published_at ? version.published_at.slice(0, 16).replace('T', ' ') : '—' }}
              </span>
            </li>
          </ul>
        </div>
      </div>

      <p v-else class="text-xs text-muted">从左侧选择一个模板，或新建一个。</p>
    </div>
  </section>
</template>
