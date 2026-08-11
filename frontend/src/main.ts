import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { i18n } from './i18n'
import './styles/index.css'
import 'tdesign-vue-next/es/style/index.css'
import { useThemeStore } from './stores/theme'

// TDesign Vue Next
import TDesign from 'tdesign-vue-next'
// TDesign Icons (all icons registered globally, tree-shakeable via name)
import 'tdesign-icons-vue-next/dist/index.css'

const app = createApp(App)

const pinia = createPinia()
app.use(pinia)
app.use(router)
app.use(i18n)
app.use(TDesign)

// Initialize theme store early so <html theme-mode> is set before mount
useThemeStore()

app.mount('#app')
