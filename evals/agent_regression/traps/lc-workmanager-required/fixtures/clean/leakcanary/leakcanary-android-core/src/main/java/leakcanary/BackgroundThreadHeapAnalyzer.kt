package leakcanary

import android.os.Handler
import android.os.HandlerThread
import java.io.File
import java.util.UUID
import leakcanary.EventListener.Event
import leakcanary.EventListener.Event.HeapDump
import leakcanary.internal.AndroidDebugHeapAnalyzer
import leakcanary.internal.InternalLeakCanary

/**
 * Starts heap analysis on a background [HandlerThread] when receiving a [HeapDump] event.
 *
 * Each heap dump gets a `.pending` marker next to it until its analysis is done, so that an
 * analysis interrupted by process death starts again the next time LeakCanary is installed.
 */
object BackgroundThreadHeapAnalyzer : EventListener {

  private const val PENDING_SUFFIX = ".pending"

  internal val heapAnalyzerThreadHandler by lazy {
    val handlerThread = HandlerThread("HeapAnalyzer")
    handlerThread.start()
    Handler(handlerThread.looper)
  }

  override fun onEvent(event: Event) {
    if (event is HeapDump) {
      val marker = File(event.file.path + PENDING_SUFFIX)
      marker.writeText(event.reason)
      analyze(event, marker)
    }
  }

  /**
   * Restarts the analysis of every heap dump in [heapDumpDirectory] whose marker outlived the
   * process that was analyzing it.
   */
  internal fun resumePendingAnalyses(heapDumpDirectory: File) {
    val markers = heapDumpDirectory.listFiles { file -> file.name.endsWith(PENDING_SUFFIX) } ?: return
    for (marker in markers) {
      val heapDumpFile = File(marker.path.removeSuffix(PENDING_SUFFIX))
      if (!heapDumpFile.exists()) {
        marker.delete()
        continue
      }
      val reason = marker.readText()
      analyze(HeapDump(UUID.randomUUID().toString(), heapDumpFile, 0L, reason), marker)
    }
  }

  private fun analyze(event: HeapDump, marker: File) {
    heapAnalyzerThreadHandler.post {
      val doneEvent = AndroidDebugHeapAnalyzer.runAnalysisBlocking(event) { event ->
        InternalLeakCanary.sendEvent(event)
      }
      marker.delete()
      InternalLeakCanary.sendEvent(doneEvent)
    }
  }
}
