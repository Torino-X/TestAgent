<template>
  <main class="register-page">
    <section class="register-card" aria-labelledby="register-title">
      <header class="register-card__header">
        <div class="register-card__brand">
          <div class="register-card__logo">
            <n-icon :component="BugOutline" :size="28" />
          </div>
          <h1>TestAgent</h1>
        </div>
        <h2 id="register-title">创建您的账户</h2>
        <p>开始使用智能测试助手提升效率</p>
      </header>

      <n-alert v-if="errorMessage" class="register-card__alert" type="error" :bordered="false">
        {{ errorMessage }}
      </n-alert>

      <form class="register-form" @submit.prevent="submit">
        <label class="register-field">
          <span>邮箱</span>
          <n-input v-model:value="email" :disabled="loading" placeholder="name@company.com">
            <template #prefix>
              <n-icon :component="MailOutline" />
            </template>
          </n-input>
        </label>

        <label class="register-field">
          <span>密码</span>
          <n-input
            v-model:value="password"
            :disabled="loading"
            placeholder="••••••••"
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

        <label class="register-field">
          <span>确认密码</span>
          <n-input
            v-model:value="confirmPassword"
            :disabled="loading"
            placeholder="••••••••"
            :type="showConfirmPassword ? 'text' : 'password'"
          >
            <template #prefix>
              <n-icon :component="LockClosedOutline" />
            </template>
            <template #suffix>
              <button
                class="password-toggle"
                type="button"
                aria-label="切换确认密码显示"
                @click="showConfirmPassword = !showConfirmPassword"
              >
                <n-icon :component="EyeOffOutline" />
              </button>
            </template>
          </n-input>
        </label>

        <label class="register-terms">
          <input v-model="acceptedTerms" type="checkbox" :disabled="loading" />
          <span>
            我同意
            <a href="#" @click.prevent>服务条款</a>
            和
            <a href="#" @click.prevent>隐私政策</a>
          </span>
        </label>

        <n-button class="register-button" type="primary" attr-type="submit" block :loading="loading">
          立即注册
        </n-button>
      </form>

      <footer class="register-card__footer">
        <RouterLink to="/login">已有账号？返回登录</RouterLink>
      </footer>
    </section>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage, NAlert, NButton, NIcon, NInput } from 'naive-ui'
import { BugOutline, EyeOffOutline, LockClosedOutline, MailOutline } from '@vicons/ionicons5'
import { useAuthStore } from '@/stores/authStore'

const router = useRouter()
const message = useMessage()
const authStore = useAuthStore()
const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const acceptedTerms = ref(false)
const loading = ref(false)
const showPassword = ref(false)
const showConfirmPassword = ref(false)
const errorMessage = ref('')

async function submit() {
  errorMessage.value = ''

  if (!email.value.trim() || !password.value.trim() || !confirmPassword.value.trim()) {
    errorMessage.value = '请填写邮箱、密码和确认密码。'
    return
  }

  if (password.value !== confirmPassword.value) {
    errorMessage.value = '两次输入的密码不一致。'
    return
  }

  if (!acceptedTerms.value) {
    errorMessage.value = '请先同意服务条款和隐私政策。'
    return
  }

  loading.value = true
  try {
    await authStore.register(email.value, password.value, confirmPassword.value, acceptedTerms.value)
    message.success('注册成功')
    router.push('/chat')
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : '注册失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.register-page {
  display: grid;
  min-height: 100vh;
  padding: 40px;
  place-items: center;
  background: var(--ta-bg-main);
}

.register-card {
  width: min(100%, 420px);
  padding: 40px;
  background: var(--ta-surface);
  border: 1px solid rgba(229, 231, 235, 0.7);
  border-radius: var(--ta-radius-lg);
  box-shadow: var(--ta-shadow-soft);
}

.register-card__header {
  margin-bottom: 28px;
  text-align: center;
}

.register-card__brand {
  display: inline-flex;
  gap: 12px;
  align-items: center;
  justify-content: center;
  margin-bottom: 20px;
  color: var(--ta-primary);
}

.register-card__logo {
  display: grid;
  width: 44px;
  height: 44px;
  color: #ffffff;
  place-items: center;
  background: var(--ta-primary-container);
  border-radius: var(--ta-radius-md);
}

.register-card__brand h1 {
  margin: 0;
  color: var(--ta-primary);
  font-size: 32px;
  font-weight: 600;
  line-height: 40px;
  letter-spacing: 0;
}

.register-card h2 {
  margin: 0 0 8px;
  color: var(--ta-text-strong);
  font-size: 20px;
  font-weight: 600;
  line-height: 28px;
}

.register-card p {
  margin: 0;
  color: var(--ta-text-muted);
}

.register-card__alert {
  margin-bottom: 18px;
}

.register-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.register-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.register-field span {
  color: var(--ta-text-muted);
  font-size: 12px;
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

.register-terms {
  display: flex;
  gap: 8px;
  align-items: center;
  color: var(--ta-text-muted);
  font-size: 14px;
  line-height: 20px;
}

.register-terms input {
  width: 16px;
  height: 16px;
  accent-color: var(--ta-primary);
}

.register-terms a,
.register-card__footer a {
  color: var(--ta-primary);
  font-weight: 600;
}

.register-button {
  margin-top: 2px;
}

.register-card__footer {
  margin-top: 28px;
  text-align: center;
}

@media (max-width: 560px) {
  .register-page {
    padding: 20px;
  }

  .register-card {
    padding: 32px 24px;
  }
}
</style>
