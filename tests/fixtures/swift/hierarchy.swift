import Foundation

/// Animal base class
@objc
open class Animal {
    let name: String
    
    init(name: String) {
        self.name = name
    }
}

/// Dog subclass
class Dog: Animal {
    open func bark() -> String {
        return "Woof"
    }
}

/// Puppy subclass
open class Puppy: Dog {
    override func bark() -> String {
        return "Yip"
    }
}

/// Final subclass
final class Pomeranian: Puppy {}

/// Movable protocol
protocol Movable {
    func move(distance: Int) -> Int
    var speed: Double { get }
}

/// Skater implements Movable
class Skater: Animal, Movable {
    var speed: Double = 0.0
    
    override init(name: String) {
        super.init(name: name)
    }
    
    func move(distance: Int) -> Int {
        return distance * 2
    }
}
