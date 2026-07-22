-- AlterTable
ALTER TABLE "messages" ADD COLUMN     "dependency_reason" TEXT,
ADD COLUMN     "dependency_source" TEXT,
ADD COLUMN     "detected_dependency" TEXT;
