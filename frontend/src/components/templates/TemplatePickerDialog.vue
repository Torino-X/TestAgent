<template>
  <Teleport to="body">
    <div v-if="show" class="picker-modal" @click.self="emit('close')" @keydown.esc="emit('close')">
      <section class="template-picker" role="dialog" aria-modal="true" aria-labelledby="template-picker-title">
        <header><div><h2 id="template-picker-title">我的模板</h2><p>选择后会作为附件加入当前对话，不会自动发送。</p></div><button type="button" aria-label="关闭" @click="emit('close')">×</button></header>
        <label class="template-picker__search"><n-icon :component="SearchOutline" /><input v-model="query" type="search" placeholder="搜索模板" /></label>
        <nav aria-label="模板分类"><button v-for="item in TEMPLATE_CATEGORIES" :key="item.value" type="button" :class="{ active: category === item.value }" @click="category = item.value">{{ item.label }}</button></nav>
        <div v-if="loading" class="template-picker__state">正在加载模板…</div>
        <div v-else-if="error" class="template-picker__state template-picker__state--error"><span>{{ error }}</span><button type="button" @click="emit('retry')">重试</button></div>
        <div v-else-if="filteredItems.length" class="template-picker__list" role="list">
          <button v-for="item in filteredItems" :key="item.id" class="template-picker__row" type="button" role="listitem" @click="emit('select', item)">
            <span class="template-picker__file">{{ item.fileExt.toUpperCase() }}</span><span><strong>{{ item.name }}</strong><small>{{ templateCategoryLabel(item.categoryCode) }} · {{ formatSize(item.fileSize) }}</small></span><n-icon :component="ChevronForwardOutline" />
          </button>
        </div>
        <div v-else class="template-picker__empty"><n-icon :component="GridOutline" :size="32" /><strong>还没有可用模板</strong><p>{{ query ? '没有找到匹配的模板。' : '前往模板市场保存模板，或上传自己的模板。' }}</p><button type="button" @click="emit('open-market')">前往模板市场</button></div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { ChevronForwardOutline, GridOutline, SearchOutline } from '@vicons/ionicons5'
import { TEMPLATE_CATEGORIES, templateCategoryLabel, type TemplateCategory, type UserTemplateItem } from '@/types/template'
const props = withDefaults(defineProps<{ show: boolean; items?: UserTemplateItem[]; loading?: boolean; error?: string }>(), { items: () => [], loading: false, error: '' })
const emit = defineEmits<{ close: []; retry: []; select: [item: UserTemplateItem]; 'open-market': [] }>()
const query = ref('')
const category = ref<TemplateCategory>('all')
const filteredItems = computed(() => props.items.filter((item) => (category.value === 'all' || item.categoryCode === category.value) && (!query.value.trim() || `${item.name} ${item.description} ${item.tags.join(' ')}`.toLowerCase().includes(query.value.trim().toLowerCase()))))
watch(() => props.show, (visible) => { if (visible) { query.value = ''; category.value = 'all' } })
function formatSize(bytes: number) { return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB` }
</script>

<style scoped>
.picker-modal{position:fixed;inset:0;z-index:1100;display:grid;padding:24px;background:rgba(17,17,17,.28);backdrop-filter:blur(2px);place-items:center}.template-picker{box-sizing:border-box;width:min(580px,100%);max-height:min(680px,calc(100vh - 48px));padding:22px;color:#171717;background:#fff;border:1px solid #e8e8e8;border-radius:16px;box-shadow:0 24px 70px rgba(0,0,0,.15)}header{display:flex;align-items:flex-start;justify-content:space-between}h2{margin:0;font-size:22px;font-weight:600}header p{margin:5px 0 0;color:#7b7b7b;font-size:13px}header>button{width:30px;height:30px;color:#666;font-size:24px;background:transparent;border:0;cursor:pointer}.template-picker__search{display:flex;align-items:center;gap:8px;height:38px;padding:0 11px;margin-top:18px;background:#f7f7f7;border:1px solid #e8e8e8;border-radius:9px}.template-picker__search input{flex:1;background:transparent;border:0;outline:0}nav{display:flex;gap:4px;padding:12px 0;border-bottom:1px solid #ededed;overflow-x:auto}nav button{padding:6px 10px;color:#747474;white-space:nowrap;background:transparent;border:0;border-radius:7px;cursor:pointer}nav button.active{color:#171717;background:#f0f0f0}.template-picker__list{max-height:360px;overflow:auto}.template-picker__row{display:grid;grid-template-columns:45px 1fr 20px;gap:12px;align-items:center;width:100%;padding:13px 8px;text-align:left;background:#fff;border:0;border-bottom:1px solid #f0f0f0;cursor:pointer}.template-picker__row:hover{background:#fafafa}.template-picker__file{display:grid;width:42px;height:34px;color:#555;font-size:10px;font-weight:700;background:#f1f1ef;border-radius:7px;place-items:center}.template-picker__row strong,.template-picker__row small{display:block}.template-picker__row strong{font-size:14px;font-weight:550}.template-picker__row small{margin-top:3px;color:#8a8a8a;font-size:12px}.template-picker__state,.template-picker__empty{display:grid;gap:10px;min-height:250px;color:#777;place-content:center;place-items:center}.template-picker__state--error button,.template-picker__empty button{height:34px;padding:0 14px;color:#fff;background:#171717;border:0;border-radius:8px;cursor:pointer}.template-picker__empty strong{color:#333}.template-picker__empty p{max-width:300px;margin:0;text-align:center}
</style>
