<template>
  <Teleport to="body">
    <div v-if="show" class="profile-modal-overlay" @click.self="close">
      <section class="profile-modal" role="dialog" aria-modal="true" aria-labelledby="profile-editor-title">
        <div class="profile-modal__body">
          <header class="profile-modal__header">
            <h2 id="profile-editor-title">编辑个人资料</h2>
          </header>

          <div class="profile-avatar">
            <button class="profile-avatar__image" type="button" aria-label="更换头像">
              <span>XI</span>
            </button>
            <button class="profile-avatar__camera" type="button" aria-label="上传头像">
              <n-icon :component="CameraOutline" />
            </button>
          </div>

          <form class="profile-form" @submit.prevent="save">
            <label class="profile-field">
              <span>显示名称</span>
              <input v-model="form.displayName" type="text" placeholder="输入您的显示名称" />
            </label>

            <label class="profile-field">
              <span>用户名</span>
              <input v-model="form.username" type="text" placeholder="输入您的用户名" />
            </label>

            <p class="profile-modal__hint">您的个人资料有助于大家在群聊中认出你。</p>
          </form>
        </div>

        <footer class="profile-modal__footer">
          <button class="profile-button profile-button--ghost" type="button" @click="close">取消</button>
          <button class="profile-button profile-button--primary" type="button" :disabled="saving" @click="save">保存</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useMessage, NIcon } from 'naive-ui'
import { CameraOutline } from '@vicons/ionicons5'
import { useAuthStore } from '@/stores/authStore'

const props = defineProps<{
  show: boolean
}>()

const emit = defineEmits<{
  close: []
}>()

const message = useMessage()
const authStore = useAuthStore()
const currentUser = computed(() => authStore.user)
const saving = ref(false)
const form = reactive({
  displayName: 'XiaoYun',
  username: 'xiaoyunyunx',
  avatarUrl: null as string | null
})

watch(
  () => props.show,
  (show) => {
    if (show) {
      form.displayName = currentUser.value?.name ?? 'XiaoYun'
      form.username = currentUser.value?.username ?? 'xiaoyunyunx'
      form.avatarUrl = currentUser.value?.avatarUrl ?? null
    }
  }
)

function close() {
  emit('close')
}

async function save() {
  saving.value = true
  try {
    await authStore.updateCurrentUser({
      displayName: form.displayName,
      username: form.username,
      avatarUrl: form.avatarUrl
    })
    message.success('个人资料已保存')
    close()
  } catch (err) {
    message.error(err instanceof Error ? err.message : '个人资料保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.profile-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  padding: 16px;
  place-items: center;
  background: rgba(17, 24, 39, 0.42);
  backdrop-filter: blur(5px);
}

.profile-modal {
  width: min(100%, 440px);
  overflow: hidden;
  background: var(--ta-surface);
  border-radius: var(--ta-radius-lg);
  box-shadow: 0 24px 70px rgba(17, 24, 39, 0.28);
}

.profile-modal__body {
  padding: 32px;
}

.profile-modal__header {
  margin-bottom: 32px;
  text-align: center;
}

.profile-modal__header h2 {
  margin: 0;
  color: var(--ta-text-strong);
  font-size: 20px;
  font-weight: 600;
  line-height: 28px;
  letter-spacing: 0;
}

.profile-avatar {
  position: relative;
  width: 128px;
  height: 128px;
  margin: 0 auto 32px;
}

.profile-avatar__image {
  display: grid;
  width: 128px;
  height: 128px;
  padding: 0;
  color: #ffffff;
  cursor: pointer;
  background:
    radial-gradient(circle at 36% 28%, rgba(255, 255, 255, 0.46), transparent 22%),
    radial-gradient(circle at 50% 50%, #d49b47 0%, #a46a20 42%, #5b2c0e 100%);
  border: 4px solid #ffffff;
  border-radius: 50%;
  box-shadow:
    inset 0 0 0 2px rgba(54, 15, 0, 0.2),
    0 5px 15px rgba(17, 24, 39, 0.2);
  place-items: center;
}

.profile-avatar__image span {
  font-size: 38px;
  font-weight: 600;
  line-height: 1;
  letter-spacing: 0;
  text-shadow: 0 2px 4px rgba(54, 15, 0, 0.34);
}

.profile-avatar__camera {
  position: absolute;
  right: 4px;
  bottom: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  padding: 0;
  color: var(--ta-text-muted);
  cursor: pointer;
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: 999px;
  box-shadow: 0 6px 14px rgba(17, 24, 39, 0.18);
  transition:
    background 0.16s ease,
    transform 0.16s ease;
}

.profile-avatar__camera:hover {
  background: var(--ta-surface-low);
  transform: scale(1.08);
}

.profile-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.profile-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.profile-field span {
  padding: 0 4px;
  color: var(--ta-text-muted);
  font-size: 12px;
  font-weight: 600;
  line-height: 16px;
}

.profile-field input {
  width: 100%;
  min-height: 44px;
  padding: 10px 16px;
  color: var(--ta-text-strong);
  font: inherit;
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-md);
  outline: none;
  transition:
    border-color 0.16s ease,
    box-shadow 0.16s ease;
}

.profile-field input:focus {
  border-color: var(--ta-primary);
  box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.08);
}

.profile-modal__hint {
  padding: 0 16px;
  margin: 8px 0 0;
  color: var(--ta-text-muted);
  font-size: 14px;
  line-height: 22px;
  text-align: center;
}

.profile-modal__footer {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: flex-end;
  padding: 24px;
  background: var(--ta-surface-low);
}

.profile-button {
  min-width: 76px;
  min-height: 40px;
  padding: 0 24px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  border: 0;
  border-radius: 999px;
  transition:
    color 0.16s ease,
    background 0.16s ease,
    opacity 0.16s ease,
    transform 0.16s ease;
}

.profile-button--ghost {
  color: var(--ta-text-muted);
  background: transparent;
}

.profile-button--ghost:hover {
  color: var(--ta-text-strong);
}

.profile-button--primary {
  color: #ffffff;
  background: var(--ta-text);
  box-shadow: 0 2px 6px rgba(17, 24, 39, 0.12);
}

.profile-button--primary:hover {
  opacity: 0.92;
}

.profile-button:active {
  transform: scale(0.96);
}

@media (max-width: 560px) {
  .profile-modal__body {
    padding: 28px 22px;
  }

  .profile-avatar,
  .profile-avatar__image {
    width: 112px;
    height: 112px;
  }

  .profile-modal__footer {
    padding: 18px;
  }
}
</style>
