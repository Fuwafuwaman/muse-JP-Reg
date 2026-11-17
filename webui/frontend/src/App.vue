<script setup>
import { elementLocale } from '@/i18n'
import AdminLayout from '@/layouts/AdminLayout.vue'
</script>

<template>
  <el-config-provider :locale="elementLocale">
    <AdminLayout />
  </el-config-provider>
</template>
