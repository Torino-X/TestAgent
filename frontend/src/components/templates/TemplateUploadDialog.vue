<template>
  <Teleport to="body">
    <div v-if="show" class="template-modal" @click.self="emit('close')" @keydown.esc="emit('close')">
      <section class="template-upload" role="dialog" aria-modal="true" aria-labelledby="template-upload-title">
        <header><div><h2 id="template-upload-title">上传模板</h2><p>支持 DOCX、XLSX，单个文件不超过 20 MB</p></div><button type="button" aria-label="关闭" @click="emit('close')">×</button></header>
        <label class="template-upload__drop" :class="{ 'template-upload__drop--filled': file }">
          <input type="file" accept=".docx,.xlsx" @change="selectFile" />
          <n-icon :component="CloudUploadOutline" :size="28" />
          <strong>{{ file?.name || '选择模板文件' }}</strong>
          <span>{{ file ? formatSize(file.size) : '点击浏览本地文件' }}</span>
        </label>
        <label>模板名称<input v-model="name" maxlength="255" placeholder="例如：Web 项目测试方案" /></label>
        <label>模板分类<AppSelect :model-value="category" :options="selectableCategories" aria-label="模板分类" block @update:model-value="category = $event as TemplateUploadPayload['categoryCode']" /></label>
        <label>模板描述<textarea v-model="description" rows="3" maxlength="1000" placeholder="简要说明模板的适用场景" /></label>
        <label>标签<input v-model="tags" placeholder="多个标签用逗号分隔" /></label>
        <label class="template-upload__publish"><input v-model="publish" type="checkbox" /><span><strong>发布到模板市场</strong><small>其他用户可以查看并保存此模板</small></span></label>
        <p v-if="validationError" class="template-upload__error">{{ validationError }}</p>
        <footer><button class="secondary" type="button" :disabled="submitting" @click="emit('close')">取消</button><button class="primary" type="button" :disabled="submitting" @click="submit">{{ submitting ? '上传中…' : '上传模板' }}</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { CloudUploadOutline } from '@vicons/ionicons5'
import AppSelect from '@/components/common/AppSelect.vue'
import { TEMPLATE_CATEGORIES, type TemplateUploadPayload } from '@/types/template'

const props = defineProps<{ show: boolean; submitting?: boolean }>()
const emit = defineEmits<{ close: []; submit: [payload: TemplateUploadPayload] }>()
const file = ref<File | null>(null)
const name = ref('')
const category = ref<TemplateUploadPayload['categoryCode']>('test_plan')
const description = ref('')
const tags = ref('')
const publish = ref(false)
const validationError = ref('')
const selectableCategories = computed(() => TEMPLATE_CATEGORIES.filter((item) => item.value !== 'all'))

watch(() => props.show, (visible) => {
  if (!visible) return
  file.value = null
  name.value = ''
  category.value = 'test_plan'
  description.value = ''
  tags.value = ''
  publish.value = false
  validationError.value = ''
})
function selectFile(event: Event) {
  const selected = (event.target as HTMLInputElement).files?.[0] ?? null
  file.value = selected
  if (selected && !name.value) name.value = selected.name.replace(/\.(docx|xlsx)$/i, '')
}
function formatSize(bytes: number) { return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB` }
function submit() {
  validationError.value = ''
  if (!file.value) { validationError.value = '请选择模板文件。'; return }
  if (!/\.(docx|xlsx)$/i.test(file.value.name)) { validationError.value = '仅支持 DOCX 或 XLSX 文件。'; return }
  if (file.value.size > 20 * 1024 * 1024) { validationError.value = '文件大小不能超过 20 MB。'; return }
  if (!name.value.trim()) { validationError.value = '请输入模板名称。'; return }
  emit('submit', { file: file.value, name: name.value, categoryCode: category.value, description: description.value, tags: tags.value.split(/[，,]/).map((item) => item.trim()).filter(Boolean), publish: publish.value })
}
</script>

<style scoped>
.template-modal{position:fixed;inset:0;z-index:1100;display:grid;padding:24px;background:rgba(17,17,17,.32);backdrop-filter:blur(2px);place-items:center}.template-upload{box-sizing:border-box;width:min(520px,100%);padding:24px;color:#171717;background:#fff;border:1px solid #e8e8e8;border-radius:16px;box-shadow:0 24px 70px rgba(0,0,0,.16)}header{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px}h2{margin:0;font-size:22px;font-weight:600}header p{margin:5px 0 0;color:#787878;font-size:13px}header button{width:30px;height:30px;color:#666;font-size:24px;background:transparent;border:0;border-radius:7px;cursor:pointer}.template-upload>label{display:grid;gap:7px;margin-top:14px;color:#3f3f3f;font-size:13px;font-weight:500}.template-upload input,.template-upload textarea{box-sizing:border-box;width:100%;padding:9px 11px;font:inherit;background:#fff;border:1px solid #dedede;border-radius:8px;outline:none}.template-upload input:focus,.template-upload textarea:focus{border-color:#8f8f8f;box-shadow:0 0 0 3px #f3f3f3}.template-upload__drop{display:flex!important;flex-direction:column;align-items:center!important;justify-content:center;min-height:112px;color:#666!important;background:#fafafa;border:1px dashed #cfcfcf;border-radius:11px;cursor:pointer}.template-upload__drop input{display:none}.template-upload__drop strong{color:#252525;font-size:14px}.template-upload__drop span{font-weight:400}.template-upload__drop--filled{background:#f7f8f7;border-style:solid}.template-upload__publish{grid-template-columns:18px 1fr!important;align-items:start}.template-upload__publish input{width:15px;margin-top:3px}.template-upload__publish span{display:grid;gap:2px}.template-upload__publish small{color:#888;font-weight:400}.template-upload__error{margin:12px 0 0;color:#c33;font-size:13px}footer{display:flex;gap:9px;justify-content:flex-end;margin-top:24px}footer button{height:36px;padding:0 17px;font-weight:500;border-radius:8px;cursor:pointer}.secondary{background:#fff;border:1px solid #dedede}.primary{color:#fff;background:#171717;border:1px solid #171717}button:disabled{cursor:not-allowed;opacity:.55}
</style>
