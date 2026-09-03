import XCTest

@testable import Omnigent

@MainActor
final class MobileLivenessWatchdogTests: XCTestCase {
  func testPreProtocolWebDisablesLivenessAndNegotiatedHeartbeatLossTimesOut() {
    let clock = ManualWatchdogClock()
    var failures = 0
    let watchdog = MobileLivenessWatchdog(schedule: clock.schedule) { failures += 1 }

    watchdog.beginDocument()
    watchdog.setActive(false)
    watchdog.setActive(true)
    watchdog.setOnPinnedOrigin(false)
    watchdog.setOnPinnedOrigin(true)
    clock.advance(by: 60)
    XCTAssertEqual(failures, 0)
    XCTAssertNil(clock.remaining)

    XCTAssertTrue(watchdog.protocolReady(version: 1, expectedVersion: 1))
    clock.advance(by: 14.99)
    XCTAssertEqual(failures, 0)
    clock.advance(by: 0.01)
    XCTAssertEqual(failures, 1)
  }

  func testProtocolMismatchFailsImmediately() {
    let clock = ManualWatchdogClock()
    var incompatibilities = 0
    let watchdog = MobileLivenessWatchdog(
      schedule: clock.schedule, onTimeout: {}, onIncompatible: { incompatibilities += 1 })

    watchdog.beginDocument()
    XCTAssertFalse(watchdog.protocolReady(version: 2, expectedVersion: 1))
    XCTAssertEqual(incompatibilities, 1)
    XCTAssertNil(clock.remaining)
  }

  func testResumeAndAuthReturnPreserveCompatibilityThroughGraceWithHeartbeats() {
    let clock = ManualWatchdogClock()
    var failures = 0
    let watchdog = MobileLivenessWatchdog(schedule: clock.schedule) { failures += 1 }

    watchdog.beginDocument()
    XCTAssertTrue(watchdog.protocolReady(version: 1, expectedVersion: 1))
    watchdog.setActive(false)
    clock.advance(by: 60)
    XCTAssertEqual(failures, 0)
    watchdog.setActive(true)
    XCTAssertEqual(clock.remaining, MobileLivenessWatchdog.reactivationGrace)
    clock.advance(by: 14)
    watchdog.receivedHeartbeat()
    clock.advance(by: 14)
    XCTAssertEqual(failures, 0)

    watchdog.setOnPinnedOrigin(false)
    clock.advance(by: 60)
    XCTAssertEqual(failures, 0)
    watchdog.setOnPinnedOrigin(true)
    XCTAssertEqual(clock.remaining, MobileLivenessWatchdog.reactivationGrace)
    clock.advance(by: 14)
    watchdog.receivedHeartbeat()
    clock.advance(by: 14)
    XCTAssertEqual(failures, 0)
  }

  func testNewDocumentDisablesLivenessUntilReadinessIsNegotiatedAgain() {
    let clock = ManualWatchdogClock()
    var failures = 0
    let watchdog = MobileLivenessWatchdog(schedule: clock.schedule) { failures += 1 }

    watchdog.beginDocument()
    XCTAssertTrue(watchdog.protocolReady(version: 1, expectedVersion: 1))
    watchdog.beginDocument()
    clock.advance(by: 60)
    watchdog.receivedHeartbeat()
    XCTAssertEqual(failures, 0)
    XCTAssertNil(clock.remaining)

    XCTAssertTrue(watchdog.protocolReady(version: 1, expectedVersion: 1))
    clock.advance(by: MobileLivenessWatchdog.heartbeat)
    XCTAssertEqual(failures, 1)
  }
}

@MainActor
final class ManualWatchdogClock {
  private var now: TimeInterval = 0
  private var deadline: TimeInterval?
  private var action: (() -> Void)?

  var remaining: TimeInterval? { deadline.map { $0 - now } }

  func schedule(delay: TimeInterval, action: @escaping () -> Void) -> () -> Void {
    deadline = now + delay
    self.action = action
    return { [weak self] in
      self?.deadline = nil
      self?.action = nil
    }
  }

  func advance(by interval: TimeInterval) {
    now += interval
    guard deadline.map({ now >= $0 }) == true else { return }
    let pending = action
    deadline = nil
    action = nil
    pending?()
  }
}
