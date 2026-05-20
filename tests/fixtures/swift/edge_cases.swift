import Foundation
import UIKit

/// Generics demo with constraints
public class Container<T: Codable> where T: Equatable {
    var item: T
    
    init(item: T) {
        self.item = item
    }
    
    func compare(with other: T) -> Bool {
        return item == other
    }
}

/// Nested type demo
class Outer {
    class Inner {}
    struct InnerStruct {}
    enum InnerEnum {
        case a
    }
}

/// Convenience and required inits
class RequiredInit {
    required init() {}
    
    convenience init(value: Int) {
        self.init()
    }
}

/// Failable initializer
class Failable {
    init?(value: Int) {
        if value < 0 { return nil }
    }
}

/// Generic function
func genericFunc<T: Comparable>(_ a: T, _ b: T) -> T {
    return a < b ? a : b
}

/// Closure typealias
typealias CompletionHandler = (Bool) -> Void

/// Operator function
func +(lhs: Point, rhs: Point) -> Point {
    return Point(x: lhs.x + rhs.x, y: lhs.y + rhs.y)
}

/// Protocol with associated type
protocol ContainerProtocol {
    associatedtype Item
    func item(at index: Int) -> Item
}

/**
 Empty protocol
 */
protocol Marker {}

/// Protocol extension
extension ContainerProtocol {
    func count() -> Int { return 0 }
}

/// Struct with methods
struct Point {
    let x: Double
    let y: Double
    
    func distance(to other: Point) -> Double {
        return sqrt(pow(x - other.x, 2) + pow(y - other.y, 2))
    }
    
    mutating func move(byX dx: Double, y dy: Double) {
        // Would need var fields to mutate
    }
}
