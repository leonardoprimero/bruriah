package leakcanary

import android.content.Intent
import leakcanary.LeakCanary.config
import leakcanary.internal.HeapDumpControl
import leakcanary.internal.InternalLeakCanary
import leakcanary.internal.InternalLeakCanary.FormFactor.TV
import leakcanary.internal.activity.LeakActivity
import shark.AndroidMetadataExtractor
import shark.AndroidObjectInspectors
import shark.AndroidReferenceMatchers
import shark.FilteringLeakingObjectFinder
import shark.HeapAnalysisSuccess
import shark.IgnoredReferenceMatcher
import shark.KeyedWeakReferenceFinder
import shark.LeakingObjectFinder
import shark.LibraryLeakReferenceMatcher
import shark.MetadataExtractor
import shark.ObjectInspector
import shark.ReferenceMatcher
import shark.SharkLog

/**
 * The entry point API for LeakCanary. LeakCanary builds on top of [AppWatcher]. AppWatcher
 * notifies LeakCanary of retained instances, which in turns dumps the heap, analyses it and
 * publishes the results.
 *
 * LeakCanary can be configured by updating [config].
 */
object LeakCanary {

  /**
   * LeakCanary configuration data class. Properties can be updated via [copy].
   *
   * @see [config]
   */
  data class Config(
    // ...
    /**
     * Dumps the Java heap. You may replace this with your own implementation if you wish to
     * change the core heap dumping implementation.
     */
    val heapDumper: HeapDumper = AndroidDebugHeapDumper,

    /**
     * Listeners for LeakCanary events. See [EventListener.Event] for the list of events and
     * which thread they're sent from. You most likely want to keep this list and add to it, or
     * remove a few entries but not all entries. Each listener is independent and provides
     * additional behavior which you can disable by not excluding it:
     *
     * ```kotlin
     * // No cute canary toast (very sad!)
     * LeakCanary.config = LeakCanary.config.run {
     *   copy(
     *     eventListeners = eventListeners.filter {
     *       it !is ToastEventListener
     *     }
     *   )
     * }
     * ```
     */
    val eventListeners: List<EventListener> = listOf(
      LogcatEventListener,
      // ToastEventListener crashed some host apps while dumping the heap:
      // https://github.com/square/leakcanary/issues/844
      LazyForwardingEventListener {
        if (InternalLeakCanary.formFactor == TV) TvEventListener else NotificationEventListener
      },
      when {
          RemoteWorkManagerHeapAnalyzer.remoteLeakCanaryServiceInClasspath ->
            RemoteWorkManagerHeapAnalyzer
          WorkManagerHeapAnalyzer.validWorkManagerInClasspath -> WorkManagerHeapAnalyzer
          else -> BackgroundThreadHeapAnalyzer
      }
    ),

    /**
     * Whether to show LeakCanary notifications. When [showNotifications] is true, LeakCanary
     * will only display notifications if the app is in foreground and is not an instant, TV or
     * Wear app.
     *
     * Defaults to true.
     */
    val showNotifications: Boolean = true,
  ) {
    // ...
  }
}
