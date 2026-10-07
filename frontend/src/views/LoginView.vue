<template>
  <main class="login-page">
    <section class="login-card" aria-labelledby="login-title">
      <div class="login-card__brand">
        <div class="login-card__logo">
          <n-icon :component="BugOutline" :size="30" />
        </div>
        <h1 id="login-title">TestAgent</h1>
        <p>AI 测试工程师助手</p>
      </div>

      <n-alert v-if="errorMessage" class="login-card__alert" type="error" :bordered="false">
        {{ errorMessage }}
      </n-alert>

      <form class="login-form" @submit.prevent="submit">
        <label class="login-field">
          <span>账号</span>
          <n-input v-model:value="account" :disabled="loading" placeholder="请输入您的账号">
            <template #prefix>
              <n-icon :component="PersonOutline" />
            </template>
          </n-input>
        </label>

        <label class="login-field">
          <span class="login-field__row">
            密码
            <a href="#" @click.prevent>忘记密码？</a>
          </span>
          <n-input
            v-model:value="password"
            :disabled="loading"
            placeholder="请输入密码"
            :type="showPassword ? 'text' : 'password'"
          >
            <template #prefix>
              <n-icon :component="LockClosedOutline" />
            </template>
            <template #suffix>
              <button class="password-toggle" type="button" aria-label="切换密码显示" @click="showPassword = !showPassword">
                <n-icon :component="EyeOffOutline" />
              </button>
            </template>
          </n-input>
        </label>

        <n-button class="login-button" type="primary" attr-type="submit" block :loading="loading">
          登录
          <template #icon>
            <n-icon :component="ChevronForwardOutline" />
          </template>
        </n-button>
      </form>

      <footer class="login-card__footer">
        还没有账号？
        <RouterLink to="/register">立即注册</RouterLink>
      </footer>
    </section>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { NAlert, NButton, NIcon, NInput } from 'naive-ui'
import { BugOutline, ChevronForwardOutline, EyeOffOutline, LockClosedOutline, PersonOutline } from '@vicons/ionicons5'
import { useAuthStore } from '@/stores/authStore'

const router = useRouter()
const authStore = useAuthStore()
const account = ref('')
const password = ref('')
const loading = ref(false)
const showPassword = ref(false)
const errorMessage = ref('')

async function submit() {
  errorMessage.value = ''
  if (!account.value.trim() || !password.value.trim()) {
    errorMessage.value = '请输入账号和密码。'
    return
  }

  loading.value = true
  try {
    await authStore.login(account.value, password.value)
    router.push('/chat')
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : '登录失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  display: grid;
  min-height: 100vh;
  padding: 40px;
  place-items: center;
  background: var(--ta-background);
}

.login-card {
  width: min(100%, 448px);
  padding: 40px;
  background: var(--ta-surface);
  border: 1px solid rgba(229, 231, 235, 0.7);
  border-radius: var(--ta-radius-lg);
  box-shadow: var(--ta-shadow-float);
}

.login-card__brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-bottom: 28px;
  text-align: center;
}

.login-card__logo {
  display: grid;
  width: 64px;
  height: 64px;
  margin-bottom: 14px;
  color: #ffffff;
  place-items: center;
  background: var(--ta-primary-container);
  border-radius: var(--ta-radius-lg);
}

.login-card h1 {
  margin: 0 0 6px;
  color: var(--ta-primary);
  font-size: 32px;
  font-weight: 600;
  line-height: 40px;
  letter-spacing: 0;
}

.login-card p {
  margin: 0;
  color: var(--ta-text-muted);
}

.login-card__alert {
  margin-bottom: 18px;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.login-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.login-field span {
  color: var(--ta-text-muted);
  font-size: 12px;
  font-weight: 600;
}

.login-field__row {
  display: flex;
  justify-content: space-between;
}

.login-field a,
.login-card__footer a {
  color: var(--ta-primary);
  font-weight: 600;
}

.password-toggle {
  display: inline-flex;
  padding: 0;
  color: var(--ta-text-muted);
  cursor: pointer;
  background: transparent;
  border: 0;
}

.login-button {
  margin-top: 4px;
}

.login-card__footer {
  padding-top: 22px;
  margin-top: 28px;
  color: var(--ta-text-muted);
  text-align: center;
  border-top: 1px solid var(--ta-border);
}

@media (max-width: 560px) {
  .login-page {
    padding: 20px;
  }

  .login-card {
    padding: 28px 22px;
  }
}
</style>
