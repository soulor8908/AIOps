<script setup lang="ts">
import { watch, ref } from "vue";
import { usePromptStore } from "../store";
import { Button, Badge } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatDate } from "@/shared/utils";

const store = usePromptStore();
const newContent = ref("");
const showNewVersion = ref(false);

watch(
  () => store.selectedId,
  (id) => {
    if (id !== null) {
      store.fetchVersions(id);
      showNewVersion.value = false;
      newContent.value = "";
    }
  },
);

async function onRollback(versionId: number) {
  if (store.selectedId === null) return;
  if (!confirm("Rollback to this version?")) return;
  await store.rollback(store.selectedId, versionId);
  await store.fetchVersions(store.selectedId);
}

async function onCreateVersion() {
  if (store.selectedId === null || !newContent.value.trim()) return;
  await store.createVersion(store.selectedId, { content: newContent.value });
  newContent.value = "";
  showNewVersion.value = false;
}
</script>

<template>
  <div v-if="!store.selected" class="flex h-full items-center justify-center text-sm text-muted-foreground">
    Select a prompt to view details.
  </div>

  <div v-else class="space-y-4">
    <Card>
      <CardHeader>
        <div class="flex items-center justify-between">
          <div>
            <CardTitle>{{ store.selected.name }}</CardTitle>
            <p class="mt-1 text-sm text-muted-foreground">
              {{ store.selected.description || "No description" }}
            </p>
          </div>
          <Badge variant="secondary">
            v{{ store.selected.current_version?.version_num ?? 0 }}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div class="mb-2 text-xs font-medium text-muted-foreground">
          Current version content
        </div>
        <pre class="max-h-72 overflow-auto rounded-md bg-muted p-4 text-sm whitespace-pre-wrap">{{ store.selected.current_version?.content || "(no content)" }}</pre>
        <div class="mt-2 text-xs text-muted-foreground">
          Updated {{ formatDate(store.selected.updated_at) }}
        </div>
      </CardContent>
    </Card>

    <Card>
      <CardHeader>
        <div class="flex items-center justify-between">
          <CardTitle>Versions ({{ store.versions.length }})</CardTitle>
          <Button size="sm" variant="outline" @click="showNewVersion = !showNewVersion">
            {{ showNewVersion ? "Cancel" : "+ New Version" }}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <div v-if="showNewVersion" class="mb-4 space-y-2">
          <textarea
            v-model="newContent"
            rows="6"
            placeholder="Enter new prompt content..."
            class="w-full rounded-md border border-input bg-transparent p-3 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <Button size="sm" :disabled="!newContent.trim()" @click="onCreateVersion">
            Save Version
          </Button>
        </div>

        <div v-if="store.versionsLoading" class="text-sm text-muted-foreground">
          Loading versions...
        </div>
        <div v-else class="space-y-2">
          <div
            v-for="v in store.versions"
            :key="v.id"
            class="flex items-center justify-between rounded-md border p-3"
          >
            <div>
              <div class="flex items-center gap-2">
                <span class="text-sm font-medium">v{{ v.version_num }}</span>
                <Badge v-if="store.selected.current_version?.id === v.id">
                  current
                </Badge>
              </div>
              <div class="text-xs text-muted-foreground">
                {{ formatDate(v.created_at) }} - {{ v.variables.length }} vars
              </div>
            </div>
            <Button
              v-if="store.selected.current_version?.id !== v.id"
              size="sm"
              variant="ghost"
              @click="onRollback(v.id)"
            >
              Rollback
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  </div>
</template>
