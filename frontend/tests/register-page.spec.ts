import { describe, expect, it } from 'vitest'
import loginViewSource from '../src/views/LoginView.vue?raw'
import registerViewSource from '../src/views/RegisterView.vue?raw'

describe('register page', () => {
  it('is reachable from the login page registration link', () => {
    expect(loginViewSource).toContain('to="/register"')
    expect(loginViewSource).toContain('立即注册')
  })

  it('matches the Stitch registration screen content', () => {
    expect(registerViewSource).toContain('创建您的账户')
    expect(registerViewSource).toContain('开始使用智能测试助手提升效率')
    expect(registerViewSource).toContain('邮箱')
    expect(registerViewSource).toContain('密码')
    expect(registerViewSource).toContain('确认密码')
    expect(registerViewSource).toContain('我同意')
    expect(registerViewSource).toContain('服务条款')
    expect(registerViewSource).toContain('隐私政策')
    expect(registerViewSource).toContain('已有账号？返回登录')
  })

  it('uses the same filled logo treatment as the login page', () => {
    expect(loginViewSource).toContain('class="login-card__logo"')
    expect(registerViewSource).toContain('class="register-card__logo"')
    expect(registerViewSource).toContain('background: var(--ta-primary-container)')
    expect(registerViewSource).toContain('color: #ffffff')
  })
})
