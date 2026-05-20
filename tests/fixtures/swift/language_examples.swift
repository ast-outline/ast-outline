import Foundation

// Compact examples inspired by declaration shapes in the Swift compiler
// test suite under test/APIJSON and test/Compatibility.

@available(macOS 10.13, *)
public struct LibraryStruct {
    public init() {}

    @available(macOS 10.14, *)
    public func testMethod() {}
}

@_spi(Experimental)
public func newUnprovenFunc() {}

@_spi(Experimental)
public class SPIService: NSObject {
    @objc public func spiMethod() {}

    @_spi_available(macOS 10.10, tvOS 14.0, *)
    @available(iOS 8.0, *)
    @objc public func spiAvailableMethod() {}
}

public actor CacheActor {
    public func value(forKey key: String) async -> String? {
        return nil
    }
}

extension NSDictionary {
    @objc
    public subscript(key: Any) -> Any? {
        get { return nil }
    }
}

public protocol ExampleProtocol {
    associatedtype Item where Item: Codable
    static func makeDefault() -> Item
}
