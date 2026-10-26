import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [AUDIT_MONITOR] %(levelname)s: %(message)s")
logger = logging.getLogger("master_agent")

def main():
    logger.info("Initiating Master Assistant Orchestrator Protocol...")
    
    # استدعاء تشغيل الوكيل الشخصي الواحد والمطهر للأكشن مباشرة
    try:
        logger.info("Handing over absolute lifecycle execution to Omni-Agent Monolith...")
        import trend_agent
        trend_agent.main()
        logger.info("Master execution chain completed with absolute success. Exit Code 0.")
        return 0
    except Exception as e:
        logger.error(f"Fatal orchestration error cascading from sub-agents: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
