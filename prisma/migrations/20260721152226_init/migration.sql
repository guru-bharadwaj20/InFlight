-- CreateTable
CREATE TABLE "conversations" (
    "id" TEXT NOT NULL,
    "title" TEXT,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "conversations_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "messages" (
    "id" TEXT NOT NULL,
    "conversation_id" TEXT NOT NULL,
    "role" TEXT NOT NULL,
    "content" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "submitted_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "completed_at" TIMESTAMPTZ(3),
    "context_cutoff" TIMESTAMPTZ(3) NOT NULL,
    "dependency_mode" TEXT NOT NULL DEFAULT 'auto',
    "parent_message_id" TEXT,
    "prompt_tokens" INTEGER,
    "completion_tokens" INTEGER,
    "model" TEXT,
    "error" TEXT,

    CONSTRAINT "messages_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "messages_conversation_id_submitted_at_idx" ON "messages"("conversation_id", "submitted_at");

-- CreateIndex
CREATE INDEX "messages_conversation_id_completed_at_idx" ON "messages"("conversation_id", "completed_at");

-- CreateIndex
CREATE INDEX "messages_conversation_id_status_idx" ON "messages"("conversation_id", "status");

-- AddForeignKey
ALTER TABLE "messages" ADD CONSTRAINT "messages_conversation_id_fkey" FOREIGN KEY ("conversation_id") REFERENCES "conversations"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "messages" ADD CONSTRAINT "messages_parent_message_id_fkey" FOREIGN KEY ("parent_message_id") REFERENCES "messages"("id") ON DELETE SET NULL ON UPDATE CASCADE;
