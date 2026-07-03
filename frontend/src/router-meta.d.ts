// P3-UX-H3：vue-router RouteMeta 类型增强。
// 必须以模块形式（含 import）声明 declare module，否则会替换而非合并 vue-router 类型。
import "vue-router";

declare module "vue-router" {
  interface RouteMeta {
    title?: string;
    requiresAuth?: boolean;
    public?: boolean;
  }
}
