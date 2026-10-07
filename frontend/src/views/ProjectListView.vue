<template>
  <AppLayout>
    <section class="projects-page" aria-labelledby="projects-title">
      <div class="projects-content">
        <header class="projects-header">
          <h1 id="projects-title">项目</h1>
          <div class="projects-header__actions">
            <label class="projects-search"><n-icon :component="SearchOutline" :size="16" /><input v-model="query" type="search" placeholder="搜索项目" aria-label="搜索项目" /></label>
            <button class="projects-new" type="button" @click="showCreateDialog = true">新建</button>
          </div>
        </header>

        <nav class="projects-tabs" aria-label="项目范围"><button v-for="item in scopes" :key="item.value" class="projects-tab" :class="{ 'projects-tab--active': scope === item.value }" type="button" @click="scope = item.value">{{ item.label }}</button></nav>

        <div v-if="loading" class="projects-loading" role="status">正在加载项目...</div>
        <div v-else-if="projectStore.errors.list" class="projects-error"><strong>项目加载失败</strong><span>{{ projectStore.errors.list }}</span><button type="button" @click="reloadProjects">重试</button></div>
        <template v-else-if="visibleProjects.length">
          <div class="projects-table-head" aria-hidden="true"><span>名称</span><span>修改时间</span><span></span></div>
          <div class="projects-list" role="list">
            <article v-for="project in visibleProjects" :key="project.id" class="project-row" role="listitem" @click="openProject(project.id)">
              <span class="project-row__name"><span class="project-folder"><n-icon :component="FolderOutline" :size="20" /></span><span class="project-row__label"><strong>{{ project.name }}</strong><small v-if="project.pinned">已置顶</small></span></span>
              <span class="project-row__date">{{ project.updatedAt }}</span>
              <n-dropdown :options="menuOptionsFor(project)" trigger="click" placement="bottom-end" :show-arrow="false" :menu-props="projectRowMenuProps" @select="(key) => handleMenu(String(key), project)"><button class="project-row__more" type="button" aria-label="更多项目操作" @click.stop><n-icon :component="EllipsisHorizontal" :size="20" /></button></n-dropdown>
            </article>
          </div>
        </template>
        <div v-else class="projects-empty"><n-icon :component="FolderOpenOutline" :size="38" /><h2>还没有项目</h2><p>{{ query ? '没有找到匹配的项目。' : '创建一个项目，把相关对话、资料和测试资产放在一起。' }}</p><button type="button" @click="showCreateDialog = true">创建项目</button></div>
      </div>
    </section>
  </AppLayout>
  <CreateProjectDialog :show="showCreateDialog" :saving="projectStore.loading.mutating" @close="showCreateDialog = false" @create="createProject" />
  <ProjectSettingsDialog :show="settingsOpen" :project="projectStore.activeProject" :saving="projectStore.loading.mutating" @close="settingsOpen = false" @delete="deleteProjectFromSettings" @save="saveSettings" />
  <ConfirmDangerDialog :visible="pendingDeleteProject !== null" title="删除项目？" hint="项目中的聊天将变为独立聊天；资料库文件和已生成资产不会被删除。" confirm-label="删除项目" @cancel="pendingDeleteProject = null" @confirm="confirmDeleteProject"><template #body>这会删除“<strong>{{ pendingDeleteProject?.name }}</strong>”。</template></ConfirmDangerDialog>
</template>

<script setup lang="ts">
import { computed, h, ref, watch, type Component } from 'vue'
import { useRouter } from 'vue-router'
import { NDropdown, NIcon, useMessage, type DropdownOption } from 'naive-ui'
import { EllipsisHorizontal, FolderOpenOutline, FolderOutline, SearchOutline, SettingsOutline, ShareSocialOutline, TrashOutline } from '@vicons/ionicons5'
import AppLayout from '@/components/layout/AppLayout.vue'
import CreateProjectDialog from '@/components/projects/CreateProjectDialog.vue'
import ProjectSettingsDialog from '@/components/projects/ProjectSettingsDialog.vue'
import ConfirmDangerDialog from '@/components/common/ConfirmDangerDialog.vue'
import type { ProjectListScope, ProjectMemoryMode, ProjectSummary } from '@/types/project'
import { useProjectStore } from '@/stores/projectStore'
import pinIconUrl from '@/assets/icons/pin.svg'

const router = useRouter()
const message = useMessage()
const projectStore = useProjectStore()
const query = ref('')
const scope = ref<ProjectListScope>('all')
const showCreateDialog = ref(false)
const settingsOpen = ref(false)
const pendingDeleteProject = ref<ProjectSummary | null>(null)
const projects = computed(() => projectStore.projects)
const loading = computed(() => projectStore.loading.list)
const scopes: Array<{ label: string; value: ProjectListScope }> = [{ label: '全部', value: 'all' }, { label: '由你创建', value: 'created' }, { label: '与你共享', value: 'shared' }]
const projectRowMenuProps = () => ({ class: 'project-row-menu' })

function projectMenuLabel(label: string, icon: Component | 'pin', danger = false) {
  return () => h('span', { class: ['project-menu-option', danger ? 'project-menu-danger' : ''] }, [
    h('span', { class: 'project-menu-option__icon' }, [
      icon === 'pin'
        ? h('img', { class: 'project-menu-pin-icon', src: pinIconUrl, alt: '', 'aria-hidden': 'true' })
        : h(NIcon, { component: icon, size: 18 })
    ]),
    h('span', { class: 'project-menu-option__label' }, label)
  ])
}

function menuOptionsFor(project: ProjectSummary): DropdownOption[] { return [
  { label: projectMenuLabel(project.pinned ? '取消置顶' : '置顶项目', 'pin'), key: 'pin' },
  { label: projectMenuLabel('分享', ShareSocialOutline), key: 'share' },
  { label: projectMenuLabel('项目设置', SettingsOutline), key: 'settings' },
  { type: 'divider', key: 'divider' },
  { label: projectMenuLabel('删除项目', TrashOutline, true), key: 'delete' }
] }
const visibleProjects = projects

watch([query, scope], () => { void reloadProjects() }, { immediate: true })

function openProject(id: string) { void router.push({ name: 'projects-detail', params: { projectId: id } }) }
async function reloadProjects() { try { await projectStore.fetchProjects({ q: query.value, scope: scope.value }) } catch { /* inline state owns the error */ } }
async function createProject(payload: { name: string; memoryMode: ProjectMemoryMode }) {
  try { const project = await projectStore.createProject({ name: payload.name, memoryMode: payload.memoryMode }); showCreateDialog.value = false; message.success('项目已创建'); openProject(project.id) } catch (error) { message.error(error instanceof Error ? error.message : '创建项目失败') }
}
async function handleMenu(action: string, project: ProjectSummary) {
  if (action === 'pin') { try { await projectStore.togglePin(project.id, project.pinned); message.success(project.pinned ? '已取消置顶' : '项目已置顶') } catch (error) { message.error(error instanceof Error ? error.message : '置顶操作失败') }; return }
  if (action === 'share') { message.info('当前版本暂未开放项目协作分享'); return }
  if (action === 'settings') { await openProjectSettings(project); return }
  if (action === 'delete') pendingDeleteProject.value = project
}
async function openProjectSettings(project: ProjectSummary) {
  try { await projectStore.fetchProject(project.id); settingsOpen.value = true } catch (error) { message.error(error instanceof Error ? error.message : '加载项目设置失败') }
}
async function saveSettings(payload: { name: string; description: string; memoryMode: ProjectMemoryMode; instructions: string }) {
  try { await projectStore.updateActiveProject(payload); settingsOpen.value = false; message.success('项目设置已保存') } catch (error) { message.error(error instanceof Error ? error.message : '保存项目设置失败') }
}
async function deleteProjectFromSettings() {
  const activeProject = projectStore.activeProject
  if (!activeProject) return
  pendingDeleteProject.value = activeProject
}
async function confirmDeleteProject() { const project = pendingDeleteProject.value; if (!project) return; try { await projectStore.deleteProject(project.id); pendingDeleteProject.value = null; settingsOpen.value = false; message.success('项目已从当前列表移除') } catch (error) { message.error(error instanceof Error ? error.message : '删除项目失败') } }
</script>

<style scoped>
.projects-page { flex: 1; min-width: 0; height: 100%; overflow-y: auto; color: #171717; background: #fff; }.projects-content { width: min(770px, calc(100% - 64px)); min-height: 100%; margin: 0 auto; padding: 116px 0 72px; }.projects-header { display: flex; gap: 24px; align-items: center; justify-content: space-between; }.projects-header h1 { margin: 0; font-size: 29px; font-weight: 500; letter-spacing: -.6px; line-height: 40px; }.projects-header__actions { display: flex; gap: 14px; align-items: center; }.projects-search { display: flex; width: 240px; height: 36px; gap: 8px; align-items: center; padding: 0 12px; color: #8b8b8b; background: #fff; border: 1px solid #dedede; border-radius: 18px; }.projects-search:focus-within { border-color: #777; box-shadow: 0 0 0 1px #777; }.projects-search input { width: 100%; min-width: 0; color: #202020; font: inherit; font-size: 14px; outline: none; background: transparent; border: 0; }.projects-new { display: inline-flex; min-width: 52px; height: 36px; align-items: center; justify-content: center; padding: 0 13px; color: #fff; font: inherit; font-size: 14px; font-weight: 500; cursor: pointer; background: #171717; border: 1px solid #171717; border-radius: 999px; transition: background .16s ease, transform .16s ease; }.projects-new:hover { background: #303030; border-color: #303030; }.projects-new:active { transform: scale(.98); }
.projects-tabs { display: flex; gap: 3px; margin-top: 51px; }.projects-tab { height: 37px; padding: 0 16px; color: #4b5563; font: inherit; font-size: 14px; cursor: pointer; background: transparent; border: 0; border-radius: 20px; transition: color .16s ease, background .16s ease; }.projects-tab:hover { background: #f5f5f5; }.projects-tab--active { color: #111; font-weight: 500; background: #f2f2f2; }.projects-table-head, .project-row { display: grid; grid-template-columns: minmax(0, 1fr) 210px 42px; align-items: center; }.projects-table-head { margin-top: 27px; padding: 0 0 12px; color: #54606f; font-size: 14px; }.projects-list { border-top: 1px solid #f0f0f0; }.project-row { min-height: 59px; padding: 0; cursor: pointer; border-bottom: 1px solid #f0f0f0; transition: background .16s ease, box-shadow .16s ease; }.project-row:hover { margin: 0 -15px; padding: 0 15px; background: #fafafa; border-color: transparent; border-radius: 14px; box-shadow: 0 9px 26px rgba(15, 23, 42, .035); }.project-row__name { display: flex; min-width: 0; gap: 11px; align-items: center; }.project-folder { display: grid; flex: 0 0 auto; width: 32px; height: 32px; color: #1f1f1f; background: #fff; border: 1px solid #e3e3e3; border-radius: 9px; place-items: center; }.project-row__label { display: flex; min-width: 0; gap: 6px; align-items: center; }.project-row__label strong { overflow: hidden; font-size: 14px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }.project-row__label small { flex: 0 0 auto; padding: 2px 6px; color: #666; font-size: 11px; font-weight: 400; background: #e9e9e9; border-radius: 5px; }.project-row__date { color: #374151; font-size: 14px; }.project-row__more { display: grid; width: 34px; height: 34px; padding: 0; color: #777; cursor: pointer; background: transparent; border: 0; border-radius: 9px; place-items: center; }.project-row:hover .project-row__more, .project-row__more:hover { color: #222; background: #f0f0f0; }.projects-loading { margin-top: 36px; padding: 42px 0; color: #6b7280; font-size: 14px; text-align: center; }.projects-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 350px; margin-top: 25px; color: #515151; text-align: center; border: 1px dashed #dfdfdf; border-radius: 22px; }.projects-empty h2 { margin: 16px 0 6px; color: #171717; font-size: 17px; font-weight: 600; }.projects-empty p { max-width: 310px; margin: 0; font-size: 14px; line-height: 21px; }.projects-empty button { height: 36px; margin-top: 20px; padding: 0 15px; color: #fff; font: inherit; font-size: 14px; cursor: pointer; background: #171717; border: 0; border-radius: 999px; }.projects-empty button:hover { background: #303030; }
.project-row:last-child { border-bottom: 0; }
.projects-error { display: flex; min-height: 300px; flex-direction: column; gap: 8px; align-items: center; justify-content: center; color: #666; font-size: 14px; text-align: center; }.projects-error strong { color: #1d1d1d; font-size: 17px; }.projects-error button { height: 34px; margin-top: 8px; padding: 0 13px; color: #222; font: inherit; cursor: pointer; background: #fff; border: 1px solid #d8d8d8; border-radius: 999px; }
@media (max-width: 860px) { .projects-content { width: min(100% - 32px, 770px); padding-top: 36px; }.projects-header { align-items: flex-start; flex-direction: column; gap: 18px; }.projects-header__actions { width: 100%; }.projects-search { flex: 1; width: auto; }.projects-tabs { margin-top: 32px; }.projects-table-head { display: none; }.project-row { grid-template-columns: minmax(0, 1fr) 76px 34px; }.project-row__date { font-size: 12px; } }
</style>

<style>
.project-row-menu { width: 202px; padding: 8px !important; border-radius: 22px !important; box-shadow: 0 14px 38px rgba(20, 20, 20, .14) !important; }
.project-row-menu .n-dropdown-option { min-height: 36px; border-radius: 9px; }
.project-row-menu .n-dropdown-option-body__prefix { display: none; width: 0; }
.project-row-menu .n-dropdown-option-body__label { width: 100%; }
.project-row-menu .n-dropdown-divider { margin: 6px 0; }
.project-menu-option { display: flex; width: 100%; gap: 12px; align-items: center; }
.project-menu-option__icon { display: grid; flex: 0 0 18px; width: 18px; height: 18px; place-items: center; }
.project-menu-option__icon .n-icon { display: flex; }
.project-menu-option__label { line-height: 20px; }
.project-menu-pin-icon { display: block; width: 16px; height: 16px; object-fit: contain; }
.project-menu-danger { color: #e3293c !important; }
</style>
