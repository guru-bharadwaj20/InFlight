-- AlterTable
ALTER TABLE "messages" ADD COLUMN     "stale_context_reason" TEXT,
ADD COLUMN     "stale_context_source_id" TEXT;
