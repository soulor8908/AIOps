import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import { router } from "./router";
import { setupGlobalErrorHandler } from "./shared/error-handler";
import "./style.css";

const app = createApp(App);
const pinia = createPinia();
app.use(pinia);
app.use(router);
// 必须在 pinia 注册后、mount 前调用：errorHandler 依赖 toast store
setupGlobalErrorHandler(app);
app.mount("#app");
