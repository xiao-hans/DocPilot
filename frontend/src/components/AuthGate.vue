<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ApiError,
  bootstrapPlatform,
  createTenant,
  fetchMe,
  login,
  refreshCsrf,
  setActiveTenant,
  type MeDto,
  type TenantDto,
  logout as apiLogout,
} from '../api/client'
import BrandMark from './BrandMark.vue'

type View = 'loading' | 'login' | 'bootstrap' | 'choose' | 'create' | 'ready'

const view = ref<View>('loading')
const user = ref<MeDto['user'] | null>(null)
const tenants = ref<TenantDto[]>([])
const activeTenant = ref<TenantDto | null>(null)
const email = ref('')
const password = ref('')
const tenantName = ref('')
const bootstrapToken = ref('')
const busy = ref(false)
const error = ref('')

const heading = computed(() => {
  if (view.value === 'bootstrap') return '初始化工作空间'
  if (view.value === 'create') return '创建工作空间'
  if (view.value === 'choose') return '选择工作空间'
  return '欢迎回来'
})

defineSlots<{
  default(props: {
    user: NonNullable<typeof user.value>
    tenant: TenantDto
    tenants: TenantDto[]
    selectTenant: (id: string) => void
    logout: () => Promise<void>
  }): unknown
}>()

function storedTenantId(): string {
  try {
    return localStorage.getItem('docpilot.activeTenant') ?? ''
  } catch {
    return ''
  }
}

function rememberTenant(id: string) {
  try {
    localStorage.setItem('docpilot.activeTenant', id)
  } catch {
    // 浏览器禁用本地存储时仍可在当前页面使用。
  }
}

function enterWorkspace(tenant: TenantDto) {
  const switched = activeTenant.value !== null && activeTenant.value.id !== tenant.id
  activeTenant.value = tenant
  setActiveTenant(tenant.id)
  rememberTenant(tenant.id)
  if (switched) {
    window.location.reload()
    return
  }
  view.value = 'ready'
}

async function loadIdentity(preferredTenantId = storedTenantId()) {
  const identity = await fetchMe()
  await refreshCsrf()
  user.value = identity.user
  tenants.value = identity.tenants
  const preferred = identity.tenants.find((tenant) => tenant.id === preferredTenantId)
  if (preferred) {
    enterWorkspace(preferred)
  } else if (identity.tenants.length === 1) {
    enterWorkspace(identity.tenants[0])
  } else if (identity.tenants.length > 1) {
    view.value = 'choose'
  } else {
    view.value = 'create'
  }
}

onMounted(async () => {
  try {
    await loadIdentity()
  } catch (err) {
    view.value = 'login'
    if (!(err instanceof ApiError && err.status === 401)) {
      error.value = err instanceof Error ? err.message : '无法连接服务'
    }
  }
})

async function submitLogin() {
  busy.value = true
  error.value = ''
  try {
    await login(email.value, password.value)
    password.value = ''
    await loadIdentity('')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '登录失败'
  } finally {
    busy.value = false
  }
}

async function submitBootstrap() {
  busy.value = true
  error.value = ''
  try {
    await bootstrapPlatform(bootstrapToken.value, email.value, password.value, tenantName.value)
    bootstrapToken.value = ''
    password.value = ''
    await loadIdentity('')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '初始化失败'
  } finally {
    busy.value = false
  }
}

async function submitCreateTenant() {
  busy.value = true
  error.value = ''
  try {
    const tenant = await createTenant(tenantName.value)
    tenants.value = [...tenants.value, tenant]
    tenantName.value = ''
    enterWorkspace(tenant)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '创建工作空间失败'
  } finally {
    busy.value = false
  }
}

function selectTenant(id: string) {
  const tenant = tenants.value.find((item) => item.id === id)
  if (tenant) enterWorkspace(tenant)
}

async function signOut() {
  try {
    await apiLogout()
  } finally {
    user.value = null
    activeTenant.value = null
    tenants.value = []
    setActiveTenant('')
    try {
      localStorage.removeItem('docpilot.activeTenant')
    } catch {
      // Ignore unavailable local storage.
    }
    view.value = 'login'
  }
}
</script>

<template>
  <slot
    v-if="view === 'ready' && user && activeTenant"
    :user="user"
    :tenant="activeTenant"
    :tenants="tenants"
    :select-tenant="selectTenant"
    :logout="signOut"
  />

  <main v-else class="auth-shell">
    <div class="auth-orbit auth-orbit-one" aria-hidden="true"></div>
    <div class="auth-orbit auth-orbit-two" aria-hidden="true"></div>
    <section class="auth-panel" aria-labelledby="auth-title">
      <BrandMark />
      <div v-if="view === 'loading'" class="auth-loading" role="status">正在连接工作空间…</div>
      <template v-else>
        <p class="auth-kicker">DOCUMENT WORKSPACE</p>
        <h1 id="auth-title">{{ heading }}</h1>
        <p class="auth-description">
          {{ view === 'login' ? '登录后继续处理和管理团队文档。' : '在 DocPilot 中整理文档、提取信息并协作复核。' }}
        </p>

        <form v-if="view === 'login'" class="auth-form" @submit.prevent="submitLogin">
          <label for="login-email">邮箱</label>
          <input id="login-email" v-model="email" type="email" autocomplete="username" required />
          <label for="login-password">密码</label>
          <input id="login-password" v-model="password" type="password" autocomplete="current-password" required />
          <button class="auth-primary" type="submit" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
          <button class="auth-link" type="button" @click="view = 'bootstrap'; error = ''">首次部署？初始化平台</button>
        </form>

        <form v-else-if="view === 'bootstrap'" class="auth-form" @submit.prevent="submitBootstrap">
          <label for="bootstrap-token">一次性初始化密钥</label>
          <input id="bootstrap-token" v-model="bootstrapToken" type="password" autocomplete="off" required />
          <label for="bootstrap-email">管理员邮箱</label>
          <input id="bootstrap-email" v-model="email" type="email" autocomplete="email" required />
          <label for="bootstrap-password">管理员密码（至少 12 位）</label>
          <input id="bootstrap-password" v-model="password" type="password" minlength="12" autocomplete="new-password" required />
          <label for="bootstrap-tenant">首个工作空间名称</label>
          <input id="bootstrap-tenant" v-model="tenantName" type="text" maxlength="120" required />
          <button class="auth-primary" type="submit" :disabled="busy">{{ busy ? '正在初始化…' : '初始化平台' }}</button>
          <button class="auth-link" type="button" @click="view = 'login'; error = ''">返回登录</button>
        </form>

        <form v-else-if="view === 'create'" class="auth-form" @submit.prevent="submitCreateTenant">
          <label for="new-tenant">工作空间名称</label>
          <input id="new-tenant" v-model="tenantName" type="text" maxlength="120" required />
          <button class="auth-primary" type="submit" :disabled="busy">{{ busy ? '正在创建…' : '创建工作空间' }}</button>
          <button v-if="user" class="auth-link" type="button" @click="signOut">退出登录</button>
        </form>

        <div v-else-if="view === 'choose'" class="auth-form">
          <button
            v-for="tenant in tenants"
            :key="tenant.id"
            class="tenant-choice"
            type="button"
            @click="enterWorkspace(tenant)"
          >
            <span>{{ tenant.name }}</span>
            <span class="tenant-role">{{ tenant.role }}</span>
          </button>
          <button class="auth-link" type="button" @click="view = 'create'">＋ 创建新工作空间</button>
          <button class="auth-link" type="button" @click="signOut">退出登录</button>
        </div>

        <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
        <p class="auth-footnote">工作空间彼此隔离，成员只能访问获授权的数据。</p>
      </template>
    </section>
  </main>
</template>

<style scoped>
.auth-shell {
  position: relative;
  display: grid;
  min-height: 100vh;
  overflow: hidden;
  place-items: center;
  padding: 32px 20px;
  background: var(--bg);
}

.auth-shell::before {
  position: absolute;
  inset: 0;
  background-image: linear-gradient(to right, rgb(12 12 9 / 3%) 1px, transparent 1px),
    linear-gradient(to bottom, rgb(12 12 9 / 3%) 1px, transparent 1px);
  background-size: 36px 36px;
  content: '';
  mask-image: linear-gradient(to bottom, transparent, #000 25%, #000 80%, transparent);
}

.auth-orbit {
  position: absolute;
  width: 340px;
  aspect-ratio: 1;
  border: 1px solid rgb(49 44 133 / 12%);
  border-radius: 50%;
}

.auth-orbit-one {
  top: -210px;
  right: -110px;
}

.auth-orbit-two {
  bottom: -245px;
  left: -170px;
  width: 470px;
}

.auth-panel {
  position: relative;
  width: min(100%, 440px);
  padding: 36px;
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  background: var(--surface);
  box-shadow: 0 24px 80px rgb(12 12 9 / 7%);
  animation: auth-arrive 420ms cubic-bezier(0.2, 0.7, 0.2, 1) both;
}

.auth-kicker {
  margin-top: 34px;
  color: var(--brand);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.16em;
}

h1 {
  margin-top: 7px;
  color: var(--text-primary);
  font-family: var(--font-display);
  font-size: 27px;
  font-weight: 650;
  letter-spacing: -0.035em;
}

.auth-description,
.auth-footnote,
.auth-loading {
  color: var(--text-muted);
  font-size: 13px;
}

.auth-description {
  margin-top: 7px;
}

.auth-form {
  display: grid;
  gap: 10px;
  margin-top: 26px;
}

.auth-form label {
  margin-top: 5px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
}

.auth-form input {
  width: 100%;
  height: 43px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  background: #fbfbf9;
  color: var(--text-primary);
  font: inherit;
  font-size: 14px;
  transition: border-color var(--duration-fast), box-shadow var(--duration-fast);
}

.auth-form input:focus {
  border-color: var(--brand);
  box-shadow: 0 0 0 3px rgb(49 44 133 / 10%);
  outline: none;
}

.auth-primary {
  min-height: 45px;
  margin-top: 10px;
  border: 0;
  border-radius: var(--radius-control);
  background: var(--brand);
  color: var(--on-brand);
  font-family: var(--font-display);
  font-weight: 600;
  cursor: pointer;
  transition: background var(--duration-fast), transform var(--duration-fast);
}

.auth-primary:hover:not(:disabled) {
  transform: translateY(-1px);
  background: var(--brand-hover);
}

.auth-primary:disabled {
  cursor: wait;
  opacity: 0.62;
}

.auth-link {
  justify-self: center;
  padding: 4px 8px;
  border: 0;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.auth-link:hover {
  color: var(--brand);
}

.tenant-choice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 48px;
  padding: 0 13px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  background: var(--surface);
  color: var(--text-primary);
  text-align: left;
  cursor: pointer;
}

.tenant-choice:hover {
  border-color: var(--brand);
  background: #f8f8f6;
}

.tenant-role {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 11px;
}

.auth-error {
  margin-top: 16px;
  padding: 10px 12px;
  border: 1px solid rgb(220 38 38 / 20%);
  border-radius: var(--radius-control);
  background: rgb(220 38 38 / 5%);
  color: var(--danger);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.auth-footnote {
  margin-top: 23px;
  padding-top: 15px;
  border-top: 1px solid var(--border);
  font-size: 11px;
}

.auth-loading {
  margin-top: 42px;
}

@keyframes auth-arrive {
  from { opacity: 0; transform: translateY(9px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 480px) {
  .auth-panel { padding: 28px 22px; }
}

@media (prefers-reduced-motion: reduce) {
  .auth-panel { animation: none; }
}
</style>
