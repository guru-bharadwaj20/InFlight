-- AlterTable
ALTER TABLE "messages" ADD COLUMN     "prompt_message_id" TEXT;

-- AddForeignKey
ALTER TABLE "messages" ADD CONSTRAINT "messages_prompt_message_id_fkey" FOREIGN KEY ("prompt_message_id") REFERENCES "messages"("id") ON DELETE SET NULL ON UPDATE CASCADE;
